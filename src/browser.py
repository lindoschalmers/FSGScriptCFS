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

    def _find_subassembly_select(self):
        """Return (css_selector, options_list) for the subassembly <select>, or (None, [])."""
        result = self.page.evaluate("""() => {
            const keywords = ['subassembly', 'sub_assembly', 'sub-assembly'];
            for (const sel of document.querySelectorAll('select')) {
                const id   = (sel.id   || '').toLowerCase().replace(/[\\s-]/g, '_');
                const name = (sel.name || '').toLowerCase().replace(/[\\s-]/g, '_');
                if (keywords.some(k => id.includes(k) || name.includes(k))) {
                    return {
                        selector: sel.id ? '#' + sel.id : '[name="' + sel.name + '"]',
                        options:  Array.from(sel.options).map(o => o.text.trim())
                    };
                }
            }
            return null;
        }""")
        if result:
            return result['selector'], result['options']
        return None, []

    def _fill_subassembly(self, value: str):
        selector, options = self._find_subassembly_select()
        if not selector:
            return

        if value in options:
            self.page.locator(selector).select_option(label=value, timeout=5000)
            return

        # Value not listed — look for any option whose text contains "new"
        new_opt = next(
            (o for o in options if "new" in o.strip().lower()),
            None
        )
        if new_opt is None:
            raise ValueError(
                f"Subassembly '{value}' not found and no 'New' option available. "
                f"Options: {[o for o in options if o]}"
            )

        self.page.locator(selector).select_option(label=new_opt, timeout=5000)
        time.sleep(0.8)

        # After selecting "New", find whatever text input became visible anywhere
        # in the document, excluding inputs that were already there.
        KNOWN_FIELD_IDS = [
            "DTE_Field_system", "DTE_Field_assembly",
            "DTE_Field_part", "DTE_Field_comments", "DTE_Field_quantity",
        ]
        new_input_selector = self.page.evaluate("""(knownIds) => {
            const inputs = Array.from(
                document.querySelectorAll('input[type=text], input[type=""], input:not([type])')
            );
            for (const inp of inputs) {
                if (knownIds.includes(inp.id)) continue;
                const rect = inp.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    if (inp.id)   return '#' + inp.id;
                    if (inp.name) return '[name="' + inp.name + '"]';
                }
            }
            return null;
        }""", KNOWN_FIELD_IDS)

        if not new_input_selector:
            raise RuntimeError(
                f"Could not find the text input revealed after selecting 'New' "
                f"for subassembly '{value}'"
            )
        self.page.locator(new_input_selector).fill(value)

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
            self.page.locator("#DTE_Field_assembly").dispatch_event("change")
            time.sleep(0.3)

            if item.get('subassembly'):
                self._fill_subassembly(item['subassembly'])

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
