import asyncio
import copy
import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import ValidationError

from app import flight_service as f


class FlightTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(f, "now_kst", return_value=datetime(2026, 9, 21, 12))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.base = dict(start="2026-11-01", end="2026-11-30", origins=["ICN", "GMP", "CJJ"])
        self.req = f.FlightSearchRequest(**self.base, outbound_date="2026-11-05")
        self.offer = {"price": 300000, "departure_token": "outbound-token", "flights": [{
            "departure_airport": {"id": "CJJ", "time": "2026-11-05 14:00"},
            "arrival_airport": {"id": "PVG", "time": "2026-11-05 15:00"},
            "airline": "Test Airline", "flight_number": "XX123"}]}
        f._cache.clear()

    def test_weekend_month_dates(self):
        days = f.FlightRequest(**self.base).dates()
        self.assertEqual([int(d["outbound_date"][-2:]) for d in days], [5, 6, 7, 12, 13, 14, 19, 20, 21, 26, 27])
        self.assertEqual(days[-1]["return_date"], "2026-11-30")

    def test_month_boundary_and_leap_day(self):
        req = f.FlightRequest(start="2028-02-01", end="2028-02-29", origins=["ICN"], weekend=False)
        self.assertEqual(req.dates()[-1]["return_date"], "2028-02-29")

    def test_bad_ranges_and_times(self):
        for overrides in ({"end": "2026-12-05"}, {"origins": []}, {"outbound_to": "05:00"},
                          {"return_to": "24:00"}, {"destinations": ["javascript:bad"]}, {"destinations": ["ICN"]}):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                f.FlightRequest(**(self.base | overrides))
        with self.assertRaises(ValidationError):
            f.FlightSearchRequest(**self.base, outbound_date="2026-11-04")

    def test_precise_time_and_cheongju(self):
        data = {"other_flights": [self.offer, self.offer]}
        self.assertEqual(len(f._offers(data, self.req)), 1)
        self.offer["flights"][0]["departure_airport"]["time"] = "2026-11-05 14:01"
        self.assertEqual(f._offers(data, self.req), [])
        self.assertEqual(f._times("06:30", "14:00"), "6,14,0,22")

    def test_overnight_wrong_airport_and_invalid_price(self):
        for transform in (
            lambda o: o["flights"][0]["arrival_airport"].update(time="2026-11-06 01:00"),
            lambda o: o["flights"][0]["arrival_airport"].update(time="2026-11-05 23:00"),
            lambda o: o["flights"][0]["departure_airport"].update(id="PUS"),
            lambda o: o.update(layovers=[{"overnight": True}]),
            lambda o: o.update(price=float("nan")),
        ):
            item = copy.deepcopy(self.offer)
            transform(item)
            self.assertEqual(f._offers({"best_flights": [item]}, self.req), [])

    def test_return_filters_and_total_price(self):
        req = f.FlightSearchRequest(**self.base, outbound_date="2026-11-05", departure_token="outbound-token")
        item = copy.deepcopy(self.offer)
        item["flights"][0]["departure_airport"] = {"id": "PVG", "time": "2026-11-08 20:00"}
        item["flights"][0]["arrival_airport"] = {"id": "CJJ", "time": "2026-11-08 22:55"}
        self.assertEqual(f._offers({"best_flights": [item]}, req)[0]["price"], 300000)
        item["flights"][0]["departure_airport"]["time"] = "2026-11-08 20:01"
        self.assertEqual(f._offers({"best_flights": [item]}, req), [])

    def run_provider(self, account, *, status=200, error=False):
        calls = []
        def handle(request):
            calls.append(request)
            if request.url.path == "/account.json":
                return httpx.Response(200, json=account)
            return httpx.Response(status, json={"error": "secret-key"} if error else {
                "search_metadata": {"status": "Success", "google_flights_url": "javascript:alert(1)"},
                "other_flights": [self.offer]})
        client_type = httpx.AsyncClient
        async def run():
            with patch.object(f, "settings", SimpleNamespace(serpapi_key="secret-key")), \
                 patch.object(f, "_lock", asyncio.Lock()), patch.object(f._budget, "check", return_value=None), \
                 patch.object(f.httpx, "AsyncClient", side_effect=lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw)):
                first = await f.search(self.req)
                second = await f.search(self.req)
                return first, second
        return asyncio.run(run()), calls

    def test_free_provider_cache_and_parameters(self):
        (first, second), calls = self.run_provider({"plan_monthly_price": 0, "plan_name": "Free Plan", "plan_searches_left": 10})
        self.assertEqual(len(calls), 2)
        self.assertEqual(first, second)
        self.assertEqual(first["url"], "")
        self.assertEqual(len(first["offers"]), 1)
        self.assertEqual(calls[-1].url.params["departure_id"], "CJJ,GMP,ICN")
        self.assertEqual(calls[-1].url.params["return_date"], "2026-11-08")
        self.assertEqual(calls[-1].url.params["stops"], "1")

    def test_paid_exhausted_unknown_accounts_blocked(self):
        for account in ({"plan_monthly_price": 25, "plan_name": "Starter", "plan_searches_left": 100},
                        {"plan_monthly_price": 0, "plan_name": "Free Plan", "plan_searches_left": 0}, {}):
            with self.subTest(account=account), self.assertRaises(f.FlightError):
                self.run_provider(account)

    def test_provider_errors_do_not_expose_key(self):
        with self.assertRaises(f.FlightError) as caught:
            self.run_provider({"plan_monthly_price": 0, "plan_name": "Free Plan", "plan_searches_left": 10}, status=429, error=True)
        self.assertNotIn("secret-key", str(caught.exception))

    def test_missing_key(self):
        with patch.object(f, "settings", SimpleNamespace(serpapi_key="")), self.assertRaises(f.FlightError):
            asyncio.run(f.search(self.req))

    def test_only_authorized_users_can_use_flight_apis(self):
        from fastapi.testclient import TestClient
        from app.main import app, current_user

        payloads = {"/api/flights/plan": self.base,
                    "/api/flights/search": self.base | {"outbound_date": "2026-11-05"}}
        client = TestClient(app)
        try:
            with patch.object(f, "search", new_callable=AsyncMock, return_value={"offers": []}) as search:
                for path, payload in payloads.items():
                    self.assertEqual(client.post(path, json=payload).status_code, 401)
                for approved in (False, True):
                    app.dependency_overrides[current_user] = lambda: SimpleNamespace(is_admin=False, allowed=approved, flight_allowed=False)
                    for path, payload in payloads.items():
                        self.assertEqual(client.post(path, json=payload).status_code, 403)
                search.assert_not_called()
                app.dependency_overrides[current_user] = lambda: SimpleNamespace(is_admin=True, allowed=True, flight_allowed=True)
                for path, payload in payloads.items():
                    self.assertEqual(client.post(path, json=payload).status_code, 200)
                search.assert_awaited_once()
        finally:
            app.dependency_overrides.pop(current_user, None)

    def test_admin_grant_revoke_and_persistence(self):
        from fastapi.testclient import TestClient
        from app import users, main

        with TemporaryDirectory() as folder, \
             patch.object(users, "USERS_FILE", Path(folder) / "users.json"), \
             patch.object(users, "SESSIONS_FILE", Path(folder) / "sessions.json"):
            # Existing records without the new field remain denied.
            users.USERS_FILE.write_text(json.dumps([{"id": "member", "username": "flight-test-member",
                                                     "password_hash": "unused", "approved": True}]), encoding="utf-8")
            store = users.UserStore()
            member = store.get("member")
            self.assertFalse(member.flight_allowed)
            admin = users.User(id="admin-test", username=users.settings.admin_username, password_hash="unused")
            self.assertTrue(admin.flight_allowed)
            client = TestClient(main.app)
            try:
                with patch.object(main, "user_store", store), \
                     patch.object(main, "_admin_row", side_effect=lambda u: u.admin_view()), \
                     patch.object(f, "search", new_callable=AsyncMock, return_value={"offers": []}) as search:
                    main.app.dependency_overrides[main.current_user] = lambda: member
                    self.assertEqual(client.put('/api/admin/users/member', json={"flight_approved": True}).status_code, 403)
                    self.assertFalse(member.flight_approved)
                    main.app.dependency_overrides[main.current_user] = lambda: admin
                    response = client.put('/api/admin/users/member', json={"flight_approved": True})
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()["flight_allowed"])
                    self.assertTrue(users.UserStore().get("member").flight_approved)
                    main.app.dependency_overrides[main.current_user] = lambda: member
                    payload = self.base | {"outbound_date": "2026-11-05"}
                    self.assertEqual(client.post('/api/flights/search', json=payload).status_code, 200)
                    search.assert_awaited_once()
                    member.approved = False
                    self.assertFalse(member.flight_allowed)
                    self.assertEqual(client.post('/api/flights/search', json=payload).status_code, 403)
                    member.approved = True
                    main.app.dependency_overrides[main.current_user] = lambda: admin
                    self.assertEqual(client.put('/api/admin/users/member', json={"flight_approved": False}).status_code, 200)
                    self.assertFalse(users.UserStore().get("member").flight_allowed)
                    main.app.dependency_overrides[main.current_user] = lambda: member
                    self.assertEqual(client.post('/api/flights/search', json=payload).status_code, 403)
                    self.assertEqual(client.post('/api/flights/plan', json=self.base).status_code, 403)
                    search.assert_awaited_once()
            finally:
                main.app.dependency_overrides.pop(main.current_user, None)


if __name__ == "__main__":
    unittest.main()
