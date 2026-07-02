import time
from typing import List, Dict, Optional
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

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
                time.sleep(1.0)

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

        new_opt = next((o for o in options if "new" in o.strip().lower()), None)
        if new_opt is None:
            raise ValueError(
                f"Subassembly '{value}' not found and no 'New' option available. "
                f"Options: {[o for o in options if o]}"
            )

        self.page.locator(selector).select_option(label=new_opt, timeout=5000)
        time.sleep(1.0)

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

    def _find_field_by_label(self, label_text: str, timeout: int = 10000) -> Optional[str]:
        """Return a CSS selector for a visible DTE form field's input/textarea,
        located by its label text (e.g. "Costs", "Comments (emissions)").

        DataTables Editor's generated field ids vary per editor config (we've
        seen the same-looking "Costs" field use a different id here than in
        create_sub_entries's child-table form), so guessing an id is unreliable.
        Every field is wrapped in a ".DTE_Field" div containing a <label> --
        matching on the label the user actually sees is what's actually stable.
        """
        script = """(label) => {
            const target = label.trim().toLowerCase().replace(/:$/, '');
            for (const field of document.querySelectorAll('.DTE_Field')) {
                const rect = field.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;
                const lbl = field.querySelector('label');
                if (!lbl) continue;
                const text = lbl.innerText.trim().toLowerCase().replace(/:$/, '');
                if (text !== target) continue;
                const input = field.querySelector('input, textarea');
                if (!input) continue;
                if (input.id) return '#' + input.id;
                if (input.name) return '[name="' + input.name + '"]';
            }
            return null;
        }"""
        try:
            self.page.wait_for_function(script, arg=label_text, timeout=timeout)
        except PlaywrightTimeoutError:
            return None
        return self.page.evaluate(script, label_text)

    def _close_open_child_tables(self):
        for toggle in self.page.locator("i.toggle-child.fa-folder-open").all():
            try:
                toggle.click()
                time.sleep(1.0)
            except Exception:
                pass

    def create_part(self, item: Dict):
        self._close_open_child_tables()
        self.page.get_by_role("button", name="New").first.click()
        self.page.wait_for_selector(".DTE_Action_Create")
        try:
            self.page.locator("#DTE_Field_system").select_option(label=item['system_label'])
            self.page.locator("#DTE_Field_system").dispatch_event("change")
            time.sleep(1.0)

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
            time.sleep(1.0)

            if item.get('subassembly'):
                self._fill_subassembly(item['subassembly'])

            if item['makebuy'] == 'm':
                self.page.locator("#DTE_Field_makebuy_0").check()
                self.page.locator("#DTE_Field_makebuy_0").dispatch_event("change")
            else:
                self.page.locator("#DTE_Field_makebuy_1").check()
                self.page.locator("#DTE_Field_makebuy_1").dispatch_event("change")

            # Checking makebuy can trigger the same kind of async, dependent-field
            # DOM rebuild that #DTE_Field_type does in create_sub_entries (both are
            # DTE_Field_* form-config reveals) -- settle before touching anything
            # below, same 1.5s used after the type select there.
            time.sleep(1.5)

            # :visible-scoped and filled AFTER the settle, in case the makebuy-driven
            # rebuild replaced these nodes -- mirrors create_sub_entries.
            self.page.locator("#DTE_Field_part:visible").fill(item['part'])
            if item['comments']:
                self.page.locator("#DTE_Field_comments:visible").fill(item['comments'])
            if item['quantity']:
                self.page.locator("#DTE_Field_quantity:visible").fill(item['quantity'])

            # Some System/Assembly combos (e.g. ET / Accumulator) reveal Costs,
            # Comments (costs), Emissions, and Comments (emissions) directly on
            # the "Create new entry" form -- these bars only appear for "buy"
            # items (makebuy != 'm'). Not every buy-item form has them (other
            # systems use create_sub_entries for cost/emissions data instead).
            # Their actual field ids differ from the same-looking fields in
            # create_sub_entries's child-table form (guessing #DTE_Field_costs
            # here never matched), so find them by label text instead -- if
            # "Costs" isn't found at all, this form variant doesn't have the
            # section and there's nothing to fill.
            if item['makebuy'] != 'm':
                cost_sel = self._find_field_by_label("Costs")
                if cost_sel:
                    if item.get('cost'):
                        self.page.locator(cost_sel).fill(item['cost'])
                    cost_comments_sel = self._find_field_by_label("Comments (costs)", timeout=3000)
                    if cost_comments_sel and item.get('cost_comments'):
                        self.page.locator(cost_comments_sel).fill(item['cost_comments'])
                    emissions_sel = self._find_field_by_label("Emissions", timeout=3000)
                    if emissions_sel and item.get('emissions'):
                        self.page.locator(emissions_sel).fill(item['emissions'])
                    emissions_comments_sel = self._find_field_by_label("Comments (emissions)", timeout=3000)
                    if emissions_comments_sel and item.get('emissions_comments'):
                        self.page.locator(emissions_comments_sel).fill(item['emissions_comments'])

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
                time.sleep(1.0)
            except Exception:
                pass

        # Server truncates stored part names to 25 chars; match on prefix.
        search_name = part_name[:25]
        self.page.wait_for_selector(f"#bom-table tr:has-text('{search_name}')", timeout=10000)
        row = self.page.get_by_role("row").filter(has_text=search_name).first
        row_id = row.get_attribute("id")
        row.locator("i.toggle-child").click()

        # Use aria-controls to reliably distinguish the child-table New button
        # from the main-table New button (which has aria-controls="bom-table").
        new_btn_sel = "button.buttons-create[aria-controls^='childTable_']"

        try:
            self.page.wait_for_selector(new_btn_sel, state="visible", timeout=10000)

            for entry in entries:
                self.page.locator(new_btn_sel).click()
                self.page.wait_for_selector("#DTE_Field_type:visible", timeout=10000)
                time.sleep(1.0)  # let form-init AJAX settle before touching fields

                try:
                    self.page.locator("#DTE_Field_type:visible").select_option(label=entry["type"])
                    time.sleep(1.5)  # let ReadFormFieldConfig AJAX finish and update the DOM

                    if entry.get("subtype"):
                        self.page.locator("#DTE_Field_subtype:visible").select_option(label=entry["subtype"])
                    if entry.get("subtype_name"):
                        self.page.locator("#DTE_Field_subtype_name:visible").fill(entry["subtype_name"])
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

                time.sleep(2.0)  # let DataTables reload the child table before the next entry
        finally:
            # Close any expanded child table so it doesn't leave a second "New" button
            # visible for the next create_part() call.
            try:
                self.page.locator("i.toggle-child.fa-folder-open").first.click()
                time.sleep(1.0)
            except Exception:
                pass