"""HTML pages: list, rows fragment, create form, detail."""

from __future__ import annotations

from django.test import TestCase, override_settings

from control_plane.apps.sessions_mgr.models import Session, SimulationCatalog


@override_settings(GATEWAY_PUBLIC_HOST="10.0.0.5:8080")
class PageTests(TestCase):
    def setUp(self) -> None:
        self.running = Session.objects.create(
            simulation_id="cip", slug="cip_live",
            status=Session.STATUS_RUNNING, http_port=6000, port=5000,
        )
        self.stopped = Session.objects.create(
            simulation_id="cip", slug="old_one", status=Session.STATUS_STOPPED,
        )

    def test_list_hides_stopped_by_default(self):
        r = self.client.get("/")
        self.assertContains(r, "cip_live")
        self.assertNotContains(r, "old_one")
        self.assertContains(r, "opc.tcp://10.0.0.5:8080/gateway/")

    def test_list_shows_stopped_on_request(self):
        r = self.client.get("/?include_stopped=1")
        self.assertContains(r, "old_one")

    def test_list_never_leaks_tokens(self):
        for url in ("/", "/?include_stopped=1", "/sessions/rows/?include_stopped=1"):
            r = self.client.get(url)
            self.assertNotContains(r, self.running.token)
            self.assertNotContains(r, self.stopped.token)

    def test_dashboard_link_only_when_running(self):
        r = self.client.get("/sessions/rows/?include_stopped=1")
        self.assertContains(r, 'href="/s/cip_live/"')
        self.assertNotContains(r, 'href="/s/old_one/"')

    def test_rows_fragment_carries_count(self):
        r = self.client.get("/sessions/rows/")
        self.assertContains(r, 'data-count="1"')
        self.assertNotContains(r, "<html")

    def test_detail_with_token(self):
        r = self.client.get(f"/sessions/{self.running.token}/")
        self.assertContains(r, "Stop session")
        self.assertContains(r, "Sessions → cip_live")
        self.assertContains(r, f"/api/sessions/{self.running.token}/")
        self.assertContains(r, 'href="/s/cip_live/"')

    def test_detail_of_ended_session_has_no_stop_button(self):
        r = self.client.get(f"/sessions/{self.stopped.token}/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="stop-btn"')

    def test_detail_wrong_token_404(self):
        r = self.client.get("/sessions/not-a-real-token/")
        self.assertEqual(r.status_code, 404)

    def test_create_lists_active_simulations(self):
        SimulationCatalog.objects.create(
            simulation_id="cip", display_name="CIP Wash", version="1.0",
        )
        SimulationCatalog.objects.create(
            simulation_id="hidden", display_name="Hidden Sim", version="1.0",
            is_active=False,
        )
        r = self.client.get("/sessions/new/")
        self.assertContains(r, "CIP Wash")
        self.assertNotContains(r, "Hidden Sim")

    def test_create_with_empty_catalog_explains(self):
        r = self.client.get("/sessions/new/")
        self.assertContains(r, "sync_catalog")
