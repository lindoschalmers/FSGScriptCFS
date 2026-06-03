import time
from typing import List, Dict, Optional
from playwright.sync_api import sync_playwright

class FSGBrowser:
    def __init__(self, config):
        self.config = config
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None

    def __enter__(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=False)
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.browser:
            self.browser.close()
        if self.pw:
            self.pw.stop()

    def login(self):
        if not self.config.username or not self.config.password:
            return False
        self.page.goto(self.config.login_url)
        self.page.fill("#tx-felogin-input-username", self.config.username)
        self.page.fill("#tx-felogin-input-password", self.config.password)
        self.page.click('input[name="submit"]')
        self.page.wait_for_load_state("networkidle")
        return True

    def goto_bom(self):
        self.page.goto(self.config.bom_url)

    def fetch_site_options(self, system_label: Optional[str] = None) -> List[str]:
        try:
            self.page.get_by_text("New", exact=True).click()
            self.page.wait_for_selector(".DTE_Action_Create", timeout=5000)

            if system_label:
                self.page.locator("#DTE_Field_system").select_option(label=system_label)
                self.page.locator("#DTE_Field_system").dispatch_event("change")
                time.sleep(0.5)

            options = self.page.eval_on_selector(
                "#DTE_Field_assembly",
                "el => Array.from(el.options).map(o => o.text)",
            ) or []
            self.page.keyboard.press("Escape")
            return options
        except Exception:
            return []

    def scrape_existing_parts(self, matcher) -> Dict[str, Dict]:
        try:
            self.page.wait_for_selector("#bom-table", timeout=10000)
            time.sleep(2.0)

            data = self.page.evaluate("""() => {
                const results = [];
                const table = document.querySelector('#bom-table');
                if (!table) return [];

                // 1. Get header mapping
                const ths = Array.from(table.querySelectorAll('thead th'));
                const headers = ths.map(th => th.innerText.toLowerCase().trim());

                const findIdx = (aliases) => headers.findIndex(h => aliases.some(a => h.includes(a)));

                const idxMap = {
                    assembly: findIdx(['assembly', 'asm', 'assy']),
                    part: findIdx(['part', 'name', 'designation', 'description']),
                    comments: findIdx(['comments', 'comment', 'notes']),
                };

                // 2. Scrape all rows
                const rows = table.querySelectorAll('tbody tr');
                rows.forEach(tr => {
                    if (tr.classList.contains('empty') || tr.innerText.includes('No data')) return;

                    const cells = tr.querySelectorAll('td');
                    const obj = {};

                    // ID format: 'DT_12345'
                    if (tr.id) obj['id'] = tr.id;

                    if (idxMap.assembly !== -1 && cells[idxMap.assembly]) obj['assembly'] = cells[idxMap.assembly].innerText.trim();
                    if (idxMap.part !== -1 && cells[idxMap.part]) obj['part'] = cells[idxMap.part].innerText.trim();
                    if (idxMap.comments !== -1 && cells[idxMap.comments]) obj['comments'] = cells[idxMap.comments].innerText.trim();

                    results.push(obj);
                });
                return results;
            }""")

            existing = {}
            for r in data:
                # Reliability check: Extract System from Row ID (e.g., 'DT_12345')
                sys = ""
                if r.get('id'):
                    sys = str(r.get('id')).split('_')[0].strip().upper()

                key = matcher.canonical_key(sys, r.get('assembly') or "", r.get('part') or "")
                existing[key] = r

            return existing
        except Exception:
            return {}

    def create_part(self, item: Dict):
        self.page.get_by_text("New", exact=True).click()
        self.page.wait_for_selector(".DTE_Action_Create")
        try:
            self.page.locator("#DTE_Field_system").select_option(label=item['system_label'])
            self.page.locator("#DTE_Field_system").dispatch_event("change")
            time.sleep(0.5)

            # Validate the assembly exists before spending time waiting
            available = self.page.eval_on_selector(
                "#DTE_Field_assembly",
                "el => Array.from(el.options).map(o => o.text.trim())"
            )
            if item['assembly'] not in available:
                raise ValueError(
                    f"Assembly '{item['assembly']}' not found in site dropdown. "
                    f"Available: {[o for o in available if o]}"
                )

            self.page.locator("#DTE_Field_assembly").select_option(label=item['assembly'], timeout=5000)
            self.page.locator("#DTE_Field_part").fill(item['part'])

            if item['makebuy'] == 'm':
                self.page.locator("#DTE_Field_makebuy_0").check()
            else:
                self.page.locator("#DTE_Field_makebuy_1").check()

            if item['comments']:
                self.page.locator("#DTE_Field_comments").fill(item['comments'])
            if item['quantity']:
                self.page.locator("#DTE_Field_quantity").fill(item['quantity'])

            self.page.get_by_text("Create", exact=True).click()
            self.page.wait_for_selector(".DTE_Action_Create", state="hidden", timeout=10000)

        except Exception:
            # Always dismiss the modal so the next part isn't blocked by the overlay.
            try:
                self.page.keyboard.press("Escape")
                self.page.wait_for_selector(".DTE_Action_Create", state="hidden", timeout=3000)
            except Exception:
                pass
            raise

    def create_sub_entries(self, part_name: str, entries: List[Dict]) -> None:
        if not entries:
            return

        # Close any child tables left open from previous failed calls to avoid
        # multiple .buttons-create buttons in the DOM (causes strict-mode failures).
        for toggle in self.page.locator("i.toggle-child.fa-folder-open").all():
            try:
                toggle.click()
                time.sleep(0.3)
            except Exception:
                pass

        # Server truncates stored part names to 25 chars; match on prefix.
        search_name = part_name[:25]
        row = self.page.get_by_role("row").filter(has_text=search_name).first
        row_id = row.get_attribute("id")
        row.locator("i.toggle-child").click()

        # aria-controls^='childTable_' distinguishes child New buttons from the
        # main-table New button (which has aria-controls="bom-table").
        if row_id:
            new_btn_sel = f"#{row_id} + tr.child .buttons-create"
        else:
            new_btn_sel = "button.buttons-create[aria-controls^='childTable_']"

        try:
            self.page.wait_for_selector(new_btn_sel, state="visible", timeout=10000)

            for entry in entries:
                self.page.locator(new_btn_sel).click()
                self.page.wait_for_selector("#DTE_Field_type:visible", timeout=10000)

                try:
                    # Selecting Type fires AJAX (ReadFormFieldConfig) that refreshes Subtype options.
                    # expect_response waits for that call; if it doesn't fire (same default value)
                    # the except branch just sleeps briefly instead.
                    try:
                        with self.page.expect_response(
                            lambda r: "ReadFormFieldConfig" in r.url, timeout=5000
                        ):
                            self.page.locator("#DTE_Field_type:visible").select_option(label=entry["type"])
                    except Exception:
                        time.sleep(1.0)

                    if entry.get("subtype"):
                        self.page.locator("#DTE_Field_subtype:visible").select_option(label=entry["subtype"])
                    if entry.get("subtype_name"):
                        self.page.locator("#DTE_Field_subtype_name:visible").fill(entry["subtype_name"])
                    # :visible qualifiers below avoid matching hidden main-editor fields with the same id
                    if entry.get("comments"):
                        self.page.locator("#DTE_Field_comments:visible").fill(entry["comments"])
                    if entry.get("quantity"):
                        self.page.locator("#DTE_Field_quantity:visible").fill(entry["quantity"])
                    if entry.get("cost"):
                        self.page.locator("#DTE_Field_costs:visible").fill(entry["cost"])
                    if entry.get("cost_comments"):
                        self.page.locator("#DTE_Field_comments_costs:visible").fill(entry["cost_comments"])
                    if entry.get("emissions"):
                        self.page.locator("#DTE_Field_emissions:visible").fill(entry["emissions"])
                    if entry.get("emissions_comments"):
                        self.page.locator("#DTE_Field_comments_emissions:visible").fill(entry["emissions_comments"])

                    self.page.locator("[data-dte-e='form_buttons']:visible").get_by_text("Create", exact=True).click()
                    self.page.wait_for_selector("#DTE_Field_type:visible", state="hidden", timeout=10000)
                except Exception:
                    try:
                        self.page.keyboard.press("Escape")
                        self.page.wait_for_selector("#DTE_Field_type:visible", state="hidden", timeout=3000)
                    except Exception:
                        pass
                    raise

                time.sleep(0.5)
        finally:
            # Close the child table so it doesn't accumulate and interfere with subsequent parts
            try:
                row.locator("i.toggle-child").click()
            except Exception:
                pass
