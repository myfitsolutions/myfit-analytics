import os
import unittest
from pathlib import Path

os.environ["APP_ENV"]="development";os.environ["DATABASE_URL"]="sqlite:///:memory:";os.environ["SESSION_COOKIE_SECURE"]="false"
from app.main import app, templates


ROOT=Path(__file__).resolve().parents[1]


class UIModernizationTests(unittest.TestCase):
    def test_existing_templates_load_and_share_ui_layer(self):
        pages=("login.html","onboarding.html","dashboard.html","reports.html","revenue.html","members.html","member_detail.html","imports.html")
        for page in pages:
            templates.env.get_template(page)
            self.assertIn('/static/style.css',(ROOT/"templates"/page).read_text(encoding="utf-8"))
            self.assertIn('/static/ui.js',(ROOT/"templates"/page).read_text(encoding="utf-8"))

    def test_navigation_uses_only_stable_top_level_routes(self):
        script=(ROOT/"static/ui.js").read_text(encoding="utf-8")
        route_paths={route.path for route in app.routes}
        for route in ("/dashboard","/reports","/revenue","/members","/imports"):
            self.assertIn(route,route_paths);self.assertIn(route,script)
        for label,route in (("Dashboard","/dashboard"),("Revenue","/revenue"),("Members","/members"),("Reports","/reports"),("Imports","/imports")):
            self.assertIn(f'navItem("{label}","{route}"',script)
        for label in ("Retention","Payment Recovery","Action Center"):
            self.assertNotIn(f'navItem("{label}"',script)
        for fake in ("/leads","/clients","/billing","/workflows","/forecast"):
            self.assertNotIn(fake,script)

    def test_navigation_active_state_is_route_based_and_not_scroll_driven(self):
        script=(ROOT/"static/ui.js").read_text(encoding="utf-8")
        self.assertIn('location.pathname.startsWith("/members/")?"/members":location.pathname',script)
        self.assertNotIn('location.hash',script);self.assertNotIn('hashchange',script)
        self.assertNotIn('IntersectionObserver',script);self.assertNotIn('setActiveNavigation',script)
        self.assertIn('if(currentNavigationHref()===href)link.setAttribute("aria-current","page")',script)
        self.assertIn('closeDrawer',script)

    def test_dashboard_quick_navigation_targets_existing_sections(self):
        dashboard=(ROOT/"templates/dashboard.html").read_text(encoding="utf-8")
        css=(ROOT/"static/style.css").read_text(encoding="utf-8")
        targets=(("Overview","dashboard-overview"),("Retention Health","retention-health"),("Payment Recovery","payment-recovery"),("Action Center","action-center"),("Follow-Ups","follow-ups"))
        self.assertIn('class="dashboard-quick-nav"',dashboard)
        for label,target in targets:
            self.assertIn(f'href="#{target}"',dashboard);self.assertIn(f'id="{target}"',dashboard);self.assertIn(label,dashboard)
        self.assertIn('overflow-x:auto',css);self.assertIn('.dashboard-quick-nav a:focus-visible',css)
        self.assertIn('@media (max-width:640px) { .dashboard-quick-nav',css)

    def test_dashboard_only_back_to_top_accessibility_motion_and_theme_contract(self):
        dashboard=(ROOT/"templates/dashboard.html").read_text(encoding="utf-8")
        css=(ROOT/"static/style.css").read_text(encoding="utf-8")
        self.assertIn('id="dashboard-back-to-top"',dashboard)
        self.assertIn('type="button" aria-label="Back to top" title="Back to top" hidden',dashboard)
        self.assertIn('<span aria-hidden="true">↑</span>',dashboard)
        self.assertIn('window.scrollY < 480',dashboard)
        self.assertIn('window.addEventListener("scroll", updateBackToTopVisibility, { passive: true })',dashboard)
        self.assertIn('window.scrollTo({ top: 0, behavior: reducedMotionQuery.matches ? "auto" : "smooth" })',dashboard)
        self.assertIn('window.matchMedia("(prefers-reduced-motion: reduce)")',dashboard)
        for page in ("revenue.html","members.html","member_detail.html","reports.html","imports.html","onboarding.html","login.html"):
            self.assertNotIn('dashboard-back-to-top',(ROOT/"templates"/page).read_text(encoding="utf-8"))
        self.assertIn('.dashboard-back-to-top { position:fixed; right:24px; bottom:24px;',css)
        self.assertIn('background:linear-gradient(145deg,var(--purple),var(--indigo))',css)
        self.assertIn('.dashboard-back-to-top:hover',css)
        self.assertIn('.dashboard-back-to-top:focus-visible',css)
        self.assertIn('@media (max-width:640px) { .dashboard-back-to-top { right:14px; bottom:18px; width:48px; height:48px; } }',css)

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

    def test_production_polish_rendering_contracts(self):
        dashboard=(ROOT/"templates/dashboard.html").read_text(encoding="utf-8")
        css=(ROOT/"static/style.css").read_text(encoding="utf-8")
        revenue_js=(ROOT/"static/revenue.js").read_text(encoding="utf-8")
        self.assertIn('data-trust-metadata',dashboard)
        self.assertIn('retention-member-identity',dashboard)
        self.assertIn('payment.member_name',dashboard)
        self.assertIn('action item',dashboard)
        self.assertNotIn('${data.action_count} ${memberLabel} need attention',dashboard)
        self.assertIn('.dashboard-member-link { color:var(--text); }',css)
        self.assertIn('gross_revenue_available',revenue_js)
        self.assertIn('"Not available"',revenue_js)

    def test_retention_badges_use_shared_theme_status_colors(self):
        dashboard=(ROOT/"templates/dashboard.html").read_text(encoding="utf-8")
        css=(ROOT/"static/style.css").read_text(encoding="utf-8")
        self.assertIn('retention-status crm-status crm-status-${member.status}',dashboard)
        self.assertIn('--at-risk: #fb923c',css)
        self.assertIn('--at-risk: #c2410c',css)
        for status in ('healthy','watch','at_risk','critical'):
            self.assertIn(f'.crm-status-{status}',css)
        self.assertIn('background: var(--surface-elevated)',css)
        self.assertNotIn('background: #eeeeee',css)

    def test_action_history_controls_and_activity_badges_use_theme_tokens(self):
        dashboard=(ROOT/"templates/dashboard.html").read_text(encoding="utf-8")
        member_detail=(ROOT/"templates/member_detail.html").read_text(encoding="utf-8")
        css=(ROOT/"static/style.css").read_text(encoding="utf-8")
        self.assertIn('id="action-history-filter"',dashboard)
        self.assertIn('id="activity-filter"',member_detail)
        self.assertIn('.action-history-header select,.crm-activity-header select',css)
        self.assertIn('.team-add-form select,.team-member-controls select',css)
        self.assertIn('background:var(--app-bg-soft); color:var(--text)',css)
        self.assertIn('select option { background:var(--surface-solid); color:var(--text); }',css)
        for category in ('milestone','payment','retention','attendance','follow_up'):
            self.assertIn(f'.crm-activity-badge-{category}',css)
        self.assertIn('.action-history-date,.crm-activity-date { color:var(--text); }',css)
        self.assertIn('.action-history-time,.crm-activity-time',css)

    def test_studio_settings_modal_uses_theme_aware_form_contract(self):
        dashboard=(ROOT/"templates/dashboard.html").read_text(encoding="utf-8")
        css=(ROOT/"static/style.css").read_text(encoding="utf-8")
        for control in ('setting-name','setting-timezone','setting-currency','setting-sender-name','setting-healthy','setting-watch','setting-at-risk','setting-follow-up','setting-primary-platform'):
            self.assertIn(f'id="{control}"',dashboard)
        self.assertIn('.settings-form .settings-grid input,.settings-form .settings-follow-up-label input',css)
        self.assertIn('.settings-form .settings-fieldset { border-color:var(--border); }',css)
        self.assertIn('.settings-form .settings-fieldset legend,.settings-form .data-source-settings strong { color:var(--text); }',css)
        self.assertIn('.settings-form .settings-help,.settings-form .settings-status { color:var(--text-muted); }',css)
        self.assertIn('.data-source-controls select',css)
        self.assertIn('input:disabled,select:disabled,textarea:disabled,input[readonly],textarea[readonly]',css)
        self.assertIn('-webkit-text-fill-color:var(--text-secondary)',css)
        self.assertIn('.team-add-form { border-color:var(--border); background:var(--app-bg-soft); }',css)

    def test_studio_settings_values_override_disabled_and_browser_foregrounds(self):
        css=(ROOT/"static/style.css").read_text(encoding="utf-8")
        dashboard=(ROOT/"templates/dashboard.html").read_text(encoding="utf-8")
        final_start=css.index('/* Final Studio Settings theme contract.')
        self.assertGreater(final_start,css.rindex('input:disabled,select:disabled'))
        legacy_labels=css[css.index('.settings-grid label,'):css.index('.settings-grid input,')]
        legacy_inputs=css[css.index('.settings-grid input,'):css.index('.settings-fieldset {')]
        self.assertNotIn('color:',legacy_labels);self.assertNotIn('color:',legacy_inputs)
        self.assertIn('.settings-form .settings-grid label,.settings-form .settings-follow-up-label { color:var(--text-secondary); }',css)
        final_contract=css[final_start:]
        self.assertIn('color:var(--text)',final_contract);self.assertIn('-webkit-text-fill-color:var(--text); opacity:1;',final_contract)
        self.assertIn('.settings-form .settings-grid input:disabled',css)
        self.assertIn('.settings-form .settings-grid input[readonly]',css)
        self.assertIn('.settings-form .settings-follow-up-label input:disabled',css)
        self.assertIn('.settings-form .data-source-controls select:disabled',css)
        self.assertIn('.settings-form input::placeholder { color:var(--text-muted); opacity:1; }',css)
        self.assertIn('.settings-form input:-webkit-autofill',css)
        self.assertIn('input[readonly]:-webkit-autofill',css)
        self.assertIn('.team-add-form input:disabled,.team-add-form input[readonly],.team-add-form select:disabled',css)
        self.assertIn('field.readOnly = !canEditSettings',dashboard)
        self.assertNotIn('field.disabled = !canEditSettings',dashboard)
        self.assertIn('/static/style.css?v=settings-theme-v3',dashboard)
        for number_id in ('setting-healthy','setting-watch','setting-at-risk','setting-follow-up'):
            self.assertIn(f'id="{number_id}" type="number"',dashboard)

    def test_revenue_import_card_explains_platform_prerequisite(self):
        template=templates.env.get_template("imports.html")
        context={"studio_id":1,"studio_name":"Test","user_email":"owner@test","user_role":"owner"}
        for platform in (None,"bsport","other"):
            rendered=template.render(**context,primary_platform=platform)
            self.assertIn("requires Hapana as the Primary Platform",rendered)
            self.assertIn("Revenue import unavailable",rendered)
            self.assertNotIn('data-import-type="revenue"',rendered)
        rendered=template.render(**context,primary_platform="hapana")
        self.assertIn('data-import-type="revenue"',rendered)
        self.assertIn("Import Revenue",rendered)
        self.assertNotIn("Revenue import unavailable",rendered)

    def test_reports_workspace_navigation_theme_and_mobile_contract(self):
        template=(ROOT/"templates/reports.html").read_text(encoding="utf-8")
        script=(ROOT/"static/reports.js").read_text(encoding="utf-8")
        nav=(ROOT/"static/ui.js").read_text(encoding="utf-8")
        css=(ROOT/"static/style.css").read_text(encoding="utf-8")
        self.assertIn('navItem("Reports","/reports","reports")',nav)
        self.assertIn('href="/reports" aria-current="page"',template)
        for value in ("this_month","last_month","last_3_months","last_6_months"):
            self.assertIn(f'data-reports-range="{value}"',template)
        self.assertIn('"Not available"',script);self.assertIn('RevenueTransaction',script)

    def test_reports_health_summary_theme_accessibility_and_responsive_contract(self):
        template=(ROOT/"templates/reports.html").read_text(encoding="utf-8")
        script=(ROOT/"static/reports.js").read_text(encoding="utf-8")
        css=(ROOT/"static/style.css").read_text(encoding="utf-8")
        backend=(ROOT/"app/main.py").read_text(encoding="utf-8")
        for label in ("Studio Health Summary","Management Insights"):
            self.assertIn(label,script)
        for copy in ("Latest imported records through","The selected period has not been imported yet","Select Last Month"):
            self.assertIn(copy,script)
        self.assertIn('id="reports-freshness"',template)
        self.assertIn("No booking data for this period",script)
        self.assertIn("No payment data for this period",script)
        self.assertIn("No revenue data for this period",script)
        self.assertIn('data-reports-range="custom"',template)
        self.assertIn('id="reports-start-date" type="date" required',template)
        self.assertIn('id="reports-end-date" type="date" required',template)
        self.assertIn('range:selectedRange',script)
        self.assertIn('params.set("start_date",startInput.value)',script)
        self.assertIn('params.set("end_date",endInput.value)',script)
        self.assertIn('history.replaceState',script)
        self.assertIn('.reports-custom-range { align-items:stretch; }',css)
        self.assertIn("Current retention snapshot",backend)
        for token in ("var(--surface-elevated)","var(--text)","var(--success)","var(--warning)","var(--danger)","var(--accent)"):
            self.assertIn(token,css)
        self.assertIn(".report-health-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }",css)
        self.assertIn(".report-metrics,.report-health-grid { grid-template-columns:1fr; }",css)
        self.assertIn('card.setAttribute("data-status",item.status)',script)
        self.assertIn('background:var(--surface-elevated)',css);self.assertIn('color:var(--text)',css)
        self.assertIn('@media (max-width:640px)',css);self.assertIn('aria-pressed',template)


if __name__=="__main__": unittest.main()
