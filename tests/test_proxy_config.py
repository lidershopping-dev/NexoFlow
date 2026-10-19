import importlib
import os
import sys
import types
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch


class ProxyConfigTests(unittest.TestCase):
    def setUp(self):
        self._module_snapshot = {
            name: sys.modules.get(name) for name in ("secrets_config", "proxy_config")
        }
        self._environment_patch = patch.dict(os.environ)
        self._environment_patch.start()
        self.addCleanup(self._environment_patch.stop)
        self.addCleanup(self._restore_modules)

    def _restore_modules(self):
        for name, module in self._module_snapshot.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def load_module(self, **values):
        fake = types.ModuleType("secrets_config")
        defaults = {
            "PROXY_MODE": "auto",
            "PROXY_HOST": "proxy.test.invalid",
            "PROXY_PORT": 8020,
            "PROXY_USER": "usuario",
            "PROXY_PASSWORD": "s@nh:a/complexa",
            "PROXY_BYPASS": "localhost,127.0.0.1",
            "PROXY_DETECTION_TIMEOUT": 0.1,
        }
        defaults.update(values)
        for key, value in defaults.items():
            setattr(fake, key, value)

        sys.modules["secrets_config"] = fake
        sys.modules.pop("proxy_config", None)
        return importlib.import_module("proxy_config")

    def tearDown(self) -> None:
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "NO_PROXY",
            "no_proxy",
            "WENI_PROXY_ACTIVE",
        ):
            os.environ.pop(name, None)

    def test_auto_uses_direct_when_proxy_unreachable(self) -> None:
        module = self.load_module()
        with (
            patch.object(module, "_is_reachable", return_value=False),
            patch.object(module, "_direct_internet_available", return_value=True),
            patch.object(module.time, "sleep", return_value=None),
        ):
            status = module.configure_proxy()
        self.assertFalse(status.active)
        self.assertEqual(os.environ.get("WENI_PROXY_ACTIVE"), "0")

    def test_auto_activates_when_proxy_reachable(self) -> None:
        module = self.load_module()
        with patch.object(module, "_is_reachable", return_value=True):
            status = module.configure_proxy()
        self.assertTrue(status.active)
        self.assertTrue(status.has_credentials)
        self.assertEqual(os.environ.get("WENI_PROXY_ACTIVE"), "1")
        self.assertIn("HTTP_PROXY", os.environ)
        self.assertIn("proxy.test.invalid:8020", os.environ["HTTP_PROXY"])
        self.assertNotIn("s@nh:a/complexa", os.environ["HTTP_PROXY"])
        self.assertIn("s%40nh%3Aa%2Fcomplexa", os.environ["HTTP_PROXY"])

    def test_refresh_switches_direct_to_proxy(self) -> None:
        module = self.load_module()
        with (
            patch.object(module, "_is_reachable", return_value=False),
            patch.object(module, "_direct_internet_available", return_value=True),
            patch.object(module.time, "sleep", return_value=None),
        ):
            initial = module.configure_proxy()
        self.assertFalse(initial.active)

        with patch.object(module, "_is_reachable", return_value=True):
            refreshed = module.refresh_network_route(reason="teste")

        self.assertTrue(refreshed.changed)
        self.assertFalse(refreshed.previous_active)
        self.assertTrue(refreshed.current_active)
        self.assertEqual(os.environ.get("WENI_PROXY_ACTIVE"), "1")
        self.assertEqual(refreshed.transition, "direta -> proxy")

    def test_refresh_switches_proxy_to_direct(self) -> None:
        module = self.load_module()
        with patch.object(module, "_is_reachable", return_value=True):
            initial = module.configure_proxy()
        self.assertTrue(initial.active)

        with (
            patch.object(module, "_is_reachable", return_value=False),
            patch.object(module, "_direct_internet_available", return_value=True),
            patch.object(module, "_direct_farmalider_available", return_value=True),
            patch.object(module.time, "sleep", return_value=None),
        ):
            refreshed = module.refresh_network_route(reason="teste")

        self.assertTrue(refreshed.changed)
        self.assertTrue(refreshed.previous_active)
        self.assertFalse(refreshed.current_active)
        self.assertEqual(os.environ.get("WENI_PROXY_ACTIVE"), "0")
        self.assertEqual(refreshed.transition, "proxy -> direta")

    def test_refresh_keeps_active_proxy_when_only_public_internet_is_reachable(self):
        module = self.load_module()
        with patch.object(module, "_is_reachable", return_value=True):
            module.configure_proxy()
        previous = os.environ["HTTPS_PROXY"]
        with (
            patch.object(module, "_is_reachable", return_value=False) as probe,
            patch.object(module, "_direct_internet_available", return_value=True),
            patch.object(module, "_direct_farmalider_available", return_value=False),
            patch.object(module.time, "sleep"),
        ):
            refreshed = module.refresh_network_route(reason="timeout na FarmaLíder")
        self.assertFalse(refreshed.changed)
        self.assertTrue(refreshed.current_active)
        self.assertEqual(probe.call_count, module.PROXY_DETECTION_ATTEMPTS)
        self.assertEqual(os.environ["HTTPS_PROXY"], previous)
        self.assertEqual(os.environ["WENI_PROXY_ACTIVE"], "1")
        self.assertNotEqual(os.environ["NO_PROXY"], "*")

    def test_refresh_recovers_transient_tcp_probe_without_switching_routes(self):
        module = self.load_module()
        with patch.object(module, "_is_reachable", return_value=True):
            module.configure_proxy()
        with (
            patch.object(module, "_is_reachable", side_effect=[False, True]) as probe,
            patch.object(module, "_direct_internet_available") as direct,
            patch.object(module.time, "sleep"),
        ):
            result = module.refresh_network_route()
        self.assertTrue(result.current_active)
        self.assertFalse(result.changed)
        self.assertEqual(probe.call_count, 2)
        direct.assert_not_called()

    def test_active_anonymous_proxy_is_preserved_if_direct_network_is_unavailable(self):
        module = self.load_module(PROXY_USER="", PROXY_PASSWORD="")
        with patch.object(module, "_is_reachable", return_value=True):
            module.configure_proxy()
        with (
            patch.object(module, "_is_reachable", return_value=False),
            patch.object(module, "_direct_internet_available", return_value=False),
            patch.object(module, "_direct_farmalider_available") as target,
            patch.object(module.time, "sleep"),
        ):
            result = module.refresh_network_route()
        self.assertTrue(result.current_active)
        self.assertFalse(result.changed)
        target.assert_not_called()

    def test_explicit_off_overrides_previous_active_proxy(self):
        module = self.load_module(PROXY_MODE="off")
        os.environ["WENI_PROXY_ACTIVE"] = "1"
        result = module.refresh_network_route()
        self.assertFalse(result.current_active)
        self.assertTrue(result.changed)

    def test_external_probe_uses_direct_https_head_without_secrets_or_redirects(self):
        from config import FARMALIDER_API_BASE_URL_EXTERNAL

        module = self.load_module()
        with patch.object(module.urllib.request, "build_opener") as build:
            opener = build.return_value
            opener.open.return_value.__enter__.return_value.status = 200
            self.assertTrue(module._direct_farmalider_available(0.1))
        proxy, redirect = build.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(
            redirect.redirect_request(None, None, 302, "", {}, "https://example.test")
        )
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, FARMALIDER_API_BASE_URL_EXTERNAL)
        self.assertEqual(request.get_method(), "HEAD")
        self.assertNotIn("token", request.full_url)
        self.assertIsNone(request.get_header("Authorization"))

    def test_external_probe_handles_http_errors_conservatively(self):
        from urllib.error import HTTPError

        module = self.load_module()
        for status in (301, 302, 401, 403, 404, 405, 407, 408, 429, 500, 503):
            with (
                self.subTest(status=status),
                patch.object(module.urllib.request, "build_opener") as build,
            ):
                build.return_value.open.side_effect = HTTPError(
                    "https://example.test", status, "", {}, None
                )
                self.assertEqual(
                    module._direct_farmalider_available(0.1), status in {401, 404, 405}
                )

    def test_external_probe_connection_failure_does_not_authorize_route_change(self):
        module = self.load_module()
        with patch.object(module.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = ConnectionRefusedError()
            self.assertFalse(module._direct_farmalider_available(0.1))

    def test_farmalider_preflight_retries_on_correct_route_after_timeout(self):
        from requests.exceptions import ReadTimeout

        from config import (
            FARMALIDER_API_BASE_URL_EXTERNAL,
            FARMALIDER_API_BASE_URL_PROXY,
        )
        from farmalider_client import FarmaliderClient, RequestPermit

        for direct_service_available in (False, True):
            with self.subTest(direct_service_available=direct_service_available):
                module = self.load_module()
                with patch.object(module, "_is_reachable", return_value=True):
                    module.configure_proxy()
                client = FarmaliderClient("TOKEN_SINTETICO")
                try:
                    with (
                        patch.object(module, "_is_reachable", return_value=False),
                        patch.object(
                            module, "_direct_internet_available", return_value=True
                        ),
                        patch.object(
                            module,
                            "_direct_farmalider_available",
                            return_value=direct_service_available,
                        ),
                        patch.object(module.time, "sleep"),
                        patch.object(
                            client,
                            "_request_slot",
                            side_effect=lambda _: nullcontext(RequestPermit(0, 4)),
                        ),
                        patch.object(client, "_protect_after_failure"),
                        patch.object(client, "_sleep_before_retry"),
                        patch.object(
                            client.session,
                            "get",
                            side_effect=[
                                ReadTimeout("teste"),
                                Mock(status_code=404, headers={}),
                            ],
                        ) as get,
                    ):
                        result = client.test_connection()
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["attempts"], 2)
                    expected = (
                        FARMALIDER_API_BASE_URL_EXTERNAL
                        if direct_service_available
                        else FARMALIDER_API_BASE_URL_PROXY
                    )
                    self.assertEqual(client.base_url, expected)
                    self.assertTrue(
                        get.call_args_list[0]
                        .args[0]
                        .startswith(FARMALIDER_API_BASE_URL_PROXY + "/")
                    )
                    self.assertTrue(
                        get.call_args_list[1].args[0].startswith(expected + "/")
                    )
                    self.assertEqual(
                        os.environ["WENI_PROXY_ACTIVE"],
                        "0" if direct_service_available else "1",
                    )
                finally:
                    client.close()

    def test_off_never_activates(self) -> None:
        module = self.load_module(PROXY_MODE="off")
        status = module.configure_proxy()
        self.assertFalse(status.active)
        self.assertEqual(os.environ.get("WENI_PROXY_ACTIVE"), "0")

    def test_partial_credentials_are_rejected(self) -> None:
        module = self.load_module(PROXY_PASSWORD="")
        with self.assertRaises(ValueError):
            module.configure_proxy()


if __name__ == "__main__":
    unittest.main()
