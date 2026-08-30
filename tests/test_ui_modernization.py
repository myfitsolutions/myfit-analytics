import os
import unittest
from pathlib import Path

os.environ["APP_ENV"]="development";os.environ["DATABASE_URL"]="sqlite:///:memory:";os.environ["SESSION_COOKIE_SECURE"]="false"
from app.main import app, templates


ROOT=Path(__file__).resolve().parents[1]


class UIModernizationTests(unittest.TestCase):
    def test_existing_templates_load_and_share_ui_layer(self):
        pages=("login.html","onboarding.html","dashboard.html","revenue.html","members.html","member_detail.html","imports.html")
        for page in pages:
            templates.env.get_template(page)
            self.assertIn('/static/style.css',(ROOT/"templates"/page).read_text(encoding="utf-8"))
            self.assertIn('/static/ui.js',(ROOT/"templates"/page).read_text(encoding="utf-8"))

    def test_navigation_uses_only_real_routes_and_real_dashboard_sections(self):
        script=(ROOT/"static/ui.js").read_text(encoding="utf-8")
        route_paths={route.path for route in app.routes}
        for route in ("/dashboard","/revenue","/members","/imports"):
            self.assertIn(route,route_paths);self.assertIn(route,script)
        dashboard=(ROOT/"templates/dashboard.html").read_text(encoding="utf-8")
        for anchor in ("retention-health","payment-recovery","action-center"):
            self.assertIn(f'id="{anchor}"',dashboard);self.assertIn(f'/dashboard#{anchor}',script)
        for fake in ("/leads","/clients","/billing","/workflows","/forecast"):
            self.assertNotIn(fake,script)

    def test_theme_and_mobile_navigation_controls_are_accessible(self):
        script=(ROOT/"static/ui.js").read_text(encoding="utf-8")
        self.assertIn("myfit-analytics-theme",script);self.assertIn('dataset.theme',script)
        self.assertIn('aria-controls',script);self.assertIn('aria-expanded',script);self.assertIn('aria-label',script)
        css=(ROOT/"static/style.css").read_text(encoding="utf-8")
        self.assertIn(':root[data-theme="light"]',css);self.assertIn('@media (prefers-reduced-motion: reduce)',css);self.assertIn('.mobile-nav-toggle',css)

    def test_existing_functional_hooks_remain_present(self):
        dashboard=(ROOT/"templates/dashboard.html").read_text(encoding="utf-8")
        for hook in ("action-center-filters","action-center-list","action-history-filter","payment-recovery-list","settings-form","setting-name","setting-timezone","setting-currency","setting-healthy","setting-watch","setting-at-risk","setting-follow-up"):
            self.assertIn(f'id="{hook}"',dashboard)
        revenue=(ROOT/"templates/revenue.html").read_text(encoding="utf-8")
        for hook in ("workspace-revenue-trend","transaction-filters","transaction-table","custom-range"):
            self.assertIn(f'id="{hook}"',revenue)
        members=(ROOT/"templates/members.html").read_text(encoding="utf-8")
        for hook in ("member-search","member-filters","members-list","member-import-form","booking-import-form","payment-import-form"):
            self.assertIn(f'id="{hook}"',members)

    def test_import_contracts_and_static_assets_remain_available(self):
        imports=(ROOT/"templates/imports.html").read_text(encoding="utf-8")
        for hook in ("mapping-file","preview-import","validate-import","execute-import","import-history-list","rollback-import"):
            self.assertIn(f'id="{hook}"',imports)
        static_routes={route.path for route in app.routes}
        self.assertIn("/static",static_routes)
        self.assertTrue((ROOT/"static/revenue.js").exists());self.assertTrue((ROOT/"static/member_crm.js").exists());self.assertTrue((ROOT/"static/import_history.js").exists())


if __name__=="__main__": unittest.main()
