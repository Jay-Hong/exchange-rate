"""Exact route inventory and authentication/ownership policy regression tests."""
import ast
import inspect
import unittest

from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Mount, Route

from app import config, crud
from app.main import (
    _delete_user_owned_rows,
    _fastapi_documentation_urls,
    _resolve_graph_v2_krx_visible,
    app,
    verify_admin,
)


PUBLIC = {
    ("GET", "/api/investing/{pair}", "get_a_latest_investing_rate"),
    ("GET", "/api/banks/{pair}", "get_a_pair_of_banks_rates"),
    ("GET", "/api/rates", "get_rates_for_mobile"),
    ("GET", "/api/rates/{currency}", "get_rates_by_currency"),
    ("GET", "/", "home"),
    ("GET", "/favicon.ico", "favicon"),
    ("GET", "/health", "health_check"),
    ("GET", "/api/news", "get_news"),
    ("GET", "/api/graph/{currency}", "get_graph_data"),
}

ADMIN = {
    ("GET", "/admin", "admin_page"),
    ("GET", "/admin/api/dashboard", "get_dashboard"),
    ("GET", "/admin/api/logs", "get_admin_logs"),
    ("GET", "/admin/api/download-logs", "download_logs"),
    ("GET", "/admin/api/monitor/current", "get_current_monitor_stats"),
    ("GET", "/admin/api/monitor/history", "get_monitor_history"),
    ("GET", "/admin/api/crawler/stats", "get_crawler_stats"),
    ("GET", "/admin/api/ibk-result-path", "get_ibk_result_path_status"),
    ("GET", "/admin/api/queue-status", "get_queue_status"),
    ("GET", "/admin/api/redis-status", "get_redis_status"),
    ("GET", "/admin/api/atomic-write-control-status", "get_atomic_write_control_status"),
    ("GET", "/admin/api/atomic-cutover-status", "get_atomic_cutover_status"),
    ("GET", "/admin/api/broadcast-heartbeat", "get_broadcast_heartbeat"),
    ("GET", "/admin/api/default-executor-probe", "get_default_executor_probe"),
    ("GET", "/admin/api/ws-connection-metrics", "get_ws_connection_metrics"),
    ("GET", "/admin/api/ws-auth-executor-metrics", "get_ws_auth_executor_metrics"),
    ("GET", "/admin/api/atomic-write-outcomes", "get_atomic_write_outcomes"),
    ("GET", "/admin/api/latest-mirror-outcomes", "get_latest_mirror_outcomes"),
    ("GET", "/admin/api/fx-shadow-counts", "get_fx_shadow_counts"),
    ("GET", "/admin/api/krx-status", "get_krx_status"),
    ("GET", "/admin/api/topic-status", "get_topic_status"),
    ("POST", "/admin/api/topic-status/reset", "reset_topic_status"),
    ("GET", "/admin/api/krx-finalizer-stats", "get_krx_finalizer_stats"),
    ("GET", "/admin/api/topic-status/fx", "get_fx_topic_status"),
    ("POST", "/admin/api/topic-status/fx/reset", "reset_fx_topic_status"),
    ("GET", "/admin/api/bank-investing-redis-stats", "get_bank_investing_redis_stats"),
    ("GET", "/admin/api/usdt-redis-stats", "get_usdt_redis_stats"),
    ("GET", "/admin/api/crawler-config", "get_crawler_config"),
    ("POST", "/admin/api/crawler-config", "toggle_crawler"),
}

FIREBASE_ONLY = {
    ("GET", "/api/v2/free/snapshot", "get_v2_free_snapshot"),
    ("POST", "/api/register-device", "register_device"),
    ("DELETE", "/api/register-device", "unregister_device"),
    ("GET", "/api/entitlements", "get_entitlements"),
    ("DELETE", "/api/user/me", "delete_user_account"),
}

FIREBASE_PREMIUM = {
    ("GET", "/api/v2/graph/catalog", "get_v2_graph_catalog"),
    ("GET", "/api/v2/graph/tab", "get_v2_graph_tab"),
    ("GET", "/api/v2/topics/snapshot", "get_v2_topic_snapshot"),
    ("POST", "/api/notification-settings", "create_notification_setting"),
    ("GET", "/api/notification-settings", "get_notification_settings"),
    ("PUT", "/api/notification-settings/{setting_id}", "update_notification_setting"),
    ("DELETE", "/api/notification-settings/{setting_id}", "delete_notification_setting"),
    ("POST", "/api/source-notification-settings", "create_source_notification_setting"),
    ("GET", "/api/source-notification-settings", "get_source_notification_settings"),
    ("PUT", "/api/source-notification-settings/{setting_id}", "update_source_notification_setting"),
    ("DELETE", "/api/source-notification-settings/{setting_id}", "delete_source_notification_setting"),
    ("GET", "/api/notification-logs", "get_notification_logs"),
    ("GET", "/api/source-notification-logs", "get_source_notification_logs"),
    ("POST", "/api/comparison-alerts", "create_comparison_alert"),
    ("GET", "/api/comparison-alerts", "get_comparison_alerts"),
    ("PUT", "/api/comparison-alerts/{setting_id}", "update_comparison_alert"),
    ("DELETE", "/api/comparison-alerts/{setting_id}", "delete_comparison_alert"),
    ("GET", "/api/comparison-notification-logs", "get_comparison_notification_logs"),
}

WEBHOOK = {("POST", "/webhooks/revenuecat", "revenuecat_webhook")}
WS = {("WS", "/ws", "websocket_endpoint")}
STATIC = {("MOUNT", "/static", "static")}

EXPECTED_DOCUMENTATION_PATHS = {
    "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"
}


def _application_inventory():
    inventory = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                inventory.add((method, route.path, route.endpoint.__name__))
        elif isinstance(route, APIWebSocketRoute):
            inventory.add(("WS", route.path, route.endpoint.__name__))
        elif isinstance(route, Mount):
            inventory.add(("MOUNT", route.path, route.name))
    return inventory


def _route_by_name(name):
    return next(
        route for route in app.routes
        if isinstance(route, (APIRoute, APIWebSocketRoute))
        and route.endpoint.__name__ == name
    )


class TestRouteInventory(unittest.TestCase):

    def test_every_application_route_has_exactly_one_policy_class(self):
        groups = (PUBLIC, ADMIN, FIREBASE_ONLY, FIREBASE_PREMIUM, WEBHOOK, WS, STATIC)
        classified = set().union(*groups)
        self.assertEqual(sum(map(len, groups)), len(classified), "route policy groups overlap")
        self.assertEqual(_application_inventory(), classified)

    def test_documentation_routes_match_environment_policy(self):
        actual = {
            route.path for route in app.routes
            if isinstance(route, Route) and not isinstance(route, APIRoute)
            and route.path in EXPECTED_DOCUMENTATION_PATHS
        }
        exposed = config.ENV.strip().lower() in {"development", "test"}
        self.assertEqual(actual, EXPECTED_DOCUMENTATION_PATHS if exposed else set())


class TestRouteAuthenticationPolicy(unittest.TestCase):

    def test_all_admin_routes_use_verify_admin_dependency(self):
        for _method, _path, endpoint_name in ADMIN:
            route = _route_by_name(endpoint_name)
            dependencies = {dependency.call for dependency in route.dependant.dependencies}
            self.assertIn(verify_admin, dependencies, endpoint_name)

    def test_firebase_routes_verify_token_in_handler(self):
        graph = {"get_v2_graph_catalog", "get_v2_graph_tab"}
        for _method, _path, endpoint_name in FIREBASE_ONLY | FIREBASE_PREMIUM:
            source = inspect.getsource(_route_by_name(endpoint_name).endpoint)
            if endpoint_name in graph:
                self.assertIn("_resolve_graph_v2_krx_visible", source, endpoint_name)
            else:
                self.assertIn("verify_firebase_token", source, endpoint_name)

        resolver = inspect.getsource(_resolve_graph_v2_krx_visible)
        self.assertIn("verify_firebase_token", resolver)
        self.assertIn("require_premium", resolver)

    def test_premium_routes_apply_premium_gate(self):
        graph = {"get_v2_graph_catalog", "get_v2_graph_tab"}
        for _method, _path, endpoint_name in FIREBASE_PREMIUM:
            if endpoint_name in graph:
                continue
            source = inspect.getsource(_route_by_name(endpoint_name).endpoint)
            self.assertIn("require_premium", source, endpoint_name)

    def test_websocket_injects_topic_subscribe_authorizer(self):
        source = inspect.getsource(_route_by_name("websocket_endpoint").endpoint)
        self.assertIn("authorize_subscribe=verify_ws_subscribe_token", source)

    def test_revenuecat_route_verifies_shared_secret(self):
        source = inspect.getsource(_route_by_name("revenuecat_webhook").endpoint)
        self.assertIn("verify_webhook_auth", source)

    def test_account_deletion_checks_revocation_and_filters_user_id(self):
        source = inspect.getsource(_route_by_name("delete_user_account").endpoint)
        self.assertIn("check_revoked=True", source)
        ownership_source = inspect.getsource(_delete_user_owned_rows)
        self.assertIn("model.user_id == user_id", ownership_source)

    def test_routes_never_return_raw_exception_text_in_500_responses(self):
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            tree = ast.parse(inspect.getsource(route.endpoint))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id != "HTTPException":
                    continue
                keywords = {keyword.arg: keyword.value for keyword in node.keywords}
                status_code = keywords.get("status_code")
                if not isinstance(status_code, ast.Constant) or status_code.value != 500:
                    continue
                detail = keywords.get("detail")
                self.assertIsInstance(
                    detail,
                    ast.Constant,
                    f"{route.endpoint.__name__} must not echo exception details",
                )


class TestUserOwnedCrudCalls(unittest.TestCase):
    USER_OWNED_CRUD = {
        "register_device", "delete_device",
        "create_notification_setting", "get_notification_settings",
        "get_notification_setting_by_id", "update_notification_setting",
        "delete_notification_setting", "create_source_notification_setting",
        "get_source_notification_settings", "get_source_notification_setting_by_id",
        "update_source_notification_setting", "delete_source_notification_setting",
        "get_notification_logs", "get_source_notification_logs",
        "create_comparison_alert", "get_comparison_alerts", "get_comparison_alert",
        "update_comparison_alert", "delete_comparison_alert",
        "get_comparison_notification_logs",
    }

    def test_every_user_owned_crud_call_receives_token_derived_user_id(self):
        endpoint_names = {
            endpoint for _method, _path, endpoint in FIREBASE_ONLY | FIREBASE_PREMIUM
        }
        checked = set()
        for endpoint_name in endpoint_names:
            tree = ast.parse(inspect.getsource(_route_by_name(endpoint_name).endpoint))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if not isinstance(node.func.value, ast.Name) or node.func.value.id != "crud":
                    continue
                if node.func.attr not in self.USER_OWNED_CRUD:
                    continue
                checked.add(node.func.attr)
                user_id = next((kw.value for kw in node.keywords if kw.arg == "user_id"), None)
                if user_id is None:
                    parameters = list(inspect.signature(
                        getattr(crud, node.func.attr)
                    ).parameters)
                    user_id_position = parameters.index("user_id")
                    if len(node.args) > user_id_position:
                        user_id = node.args[user_id_position]
                self.assertIsInstance(user_id, ast.Name, f"{endpoint_name}:{node.func.attr}")
                self.assertEqual(user_id.id, "user_id", f"{endpoint_name}:{node.func.attr}")
        self.assertEqual(checked, self.USER_OWNED_CRUD)


class TestProductionDocumentationPolicy(unittest.TestCase):

    def test_production_and_unknown_environments_disable_all_documentation(self):
        for environment in ("production", "staging", "", "unexpected"):
            with self.subTest(environment=environment):
                options = _fastapi_documentation_urls(environment)
                self.assertTrue(options)
                self.assertTrue(all(value is None for value in options.values()))
                probe = FastAPI(**options)
                self.assertFalse(EXPECTED_DOCUMENTATION_PATHS & {route.path for route in probe.routes})

    def test_development_and_test_keep_local_documentation(self):
        for environment in ("development", "test", " TEST "):
            with self.subTest(environment=environment):
                probe = FastAPI(**_fastapi_documentation_urls(environment))
                self.assertEqual(
                    EXPECTED_DOCUMENTATION_PATHS,
                    EXPECTED_DOCUMENTATION_PATHS & {route.path for route in probe.routes},
                )


if __name__ == "__main__":
    unittest.main()
