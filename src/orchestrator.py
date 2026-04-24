import os
import re
import time
import random
import questionary
from .config import Config
from .excel import ExcelProcessor
from .matcher import AssemblyMatcher
from .browser import FSGBrowser
from .ui import UI

class BOMAutomation:
    def __init__(self, config: Config):
        self.config = config
        self.ui = UI(config.log_file)
        self.excel = ExcelProcessor(config.boms_dir)
        self.matcher = AssemblyMatcher()

    def _smart_delay(self, seconds, jitter=0.2):
        if seconds <= 0:
            return
        actual = seconds * (1 + random.uniform(-jitter, jitter))
        time.sleep(max(0.1, actual))

    def run(self):
        self.ui.log("=" * 60)
        self.ui.log("FSG CCBOM Automation — Starting")
        
        # 1. File Selection
        files = self.excel.discover_files()
        if not files:
            self.ui.log(f"No Excel files in '{self.config.boms_dir}/'", "ERROR")
            return
            
        filepath = self.ui.prompt_ask(questionary.select("Select BOM file:", choices=[os.path.basename(f) for f in files]))
        if not filepath:
            return
        filepath = next(f for f in files if os.path.basename(f) == filepath)

        # 2. System Selection
        import pandas as pd

        # Build reverse map: normalized full name -> 2-char code
        # e.g. "brakesystem" -> "BR", "drivetrain" -> "DT"
        system_norm_map = {}
        for code, label in self.matcher.SYSTEM_MAP.items():
            parts_label = label.split(" - ", 1)
            name_part = parts_label[1] if len(parts_label) > 1 else parts_label[0]
            system_norm_map[re.sub(r"\W+", "", name_part.lower())] = code
            system_norm_map[re.sub(r"\W+", "", label.lower())] = code

        df_temp = pd.read_excel(filepath)
        df_temp.columns = [str(c).strip().lower() for c in df_temp.columns]

        if "system" not in df_temp.columns:
            self.ui.log("Excel file missing 'system' column.", "ERROR")
            return

        seen_codes = set()
        systems = []
        for s in df_temp["system"].dropna().unique():
            s_str = str(s).strip()
            if not s_str:
                continue
            norm = re.sub(r"\W+", "", s_str.lower())
            code = system_norm_map.get(norm, s_str.upper())
            if len(code) == 2 and code in self.matcher.SYSTEM_MAP and code not in seen_codes:
                systems.append(code)
                seen_codes.add(code)

        run_system = self.config.default_system
        if not run_system or run_system not in systems:
            choices = [questionary.Choice(f"{s} - {self.matcher.get_system_label(s)}", s) for s in systems]
            choices.insert(0, questionary.Choice("ALL - Process everything", "ALL"))
            run_system = self.ui.prompt_ask(questionary.select("Select system:", choices=choices))

        if not run_system:
            return

        # 3. Filter Rows
        parts, stats = self.excel.process_file(filepath, run_system, system_norm_map)
        
        # Detailed logging of Excel scan
        self.ui.log(f"Excel Scan Summary for '{os.path.basename(filepath)}':")
        self.ui.log(f"  • Total rows found:     {stats['total_excel_rows']}")
        self.ui.log(f"  • Empty/Invalid rows:   {stats['empty_rows']}")
        self.ui.log(f"  • Example rows skipped: {stats['example_rows']}")
        self.ui.log(f"  • System mismatch:      {stats['system_mismatch']} (filtered by {run_system})")
        self.ui.log(f"  • Already done (color): {stats['skipped_by_color']}")
        self.ui.log(f"  • Valid parts found:    {stats['valid_parts']}")

        if not parts:
            self.ui.log("No valid parts found after filtering. Exiting.", "WARN")
            return

        self.ui.show_summary(len(parts), os.path.basename(filepath), run_system, self.config.test_mode, self.config.dry_run)
        if not self.ui.prompt_ask(questionary.confirm("Proceed with uploading?")):
            return

        # 4. Browser Session
        with FSGBrowser(self.config) as browser:
            if not browser.login():
                self.ui.log("Manual login required. Please login and navigate to BOM page.")
            
            browser.goto_bom()
            self.ui.console.input("\nPress ENTER when ready on BOM page...")
            
            # Fetch Options & Match Assemblies
            sys_label = self.matcher.get_system_label(run_system) if run_system != "ALL" else None
            site_options = browser.fetch_site_options(sys_label)
            
            if not site_options:
                self.ui.log("Could not fetch site options from the server.", "ERROR")
                return

            # Match and Whitelist
            runtime_allowed = []
            if not self.config.allowed_assemblies:
                self.ui.log("Use SPACE to toggle assemblies, ENTER to confirm (all pre-selected).")
                choices = [questionary.Choice(title=opt, checked=True) for opt in site_options]
                runtime_allowed = self.ui.prompt_ask(questionary.checkbox("Select assemblies to process:", choices=choices))
                if not runtime_allowed:
                    self.ui.log("No assemblies selected. Exiting.")
                    return
            
            matched_parts = []
            skipped_matching = 0
            for p in parts:
                resolved = self.matcher.resolve_label(p['assembly'], site_options, runtime_allowed or self.config.allowed_assemblies)
                if resolved:
                    p['assembly'] = resolved
                    p['system_label'] = self.matcher.get_system_label(p['system'])
                    matched_parts.append(p)
                else:
                    skipped_matching += 1
            
            self.ui.log("Assembly Matching Summary:")
            self.ui.log(f"  • Parts matching selected assemblies: {len(matched_parts)}")
            self.ui.log(f"  • Parts skipped (no assembly match): {skipped_matching}")

            if not matched_parts:
                self.ui.log("No parts matched the selected assemblies. Exiting.", "ERROR")
                return

            if self.config.test_mode:
                limit = min(self.config.test_limit, len(matched_parts))
                matched_parts = matched_parts[:limit]
                self.ui.log(f"Test Mode active: will attempt to upload {limit} parts.")
            else:
                self.ui.log(f"Will attempt to upload all {len(matched_parts)} matched parts.")

            # Deduplication
            self.ui.log("Fetching existing parts from FSG for deduplication...")
            existing = browser.scrape_existing_parts(self.matcher)
            self.ui.log(f"Found {len(existing)} existing parts on the website.")

            # 5. Upload Loop
            start_time = time.time()
            live, status_table, progress, task_id = self.ui.create_dashboard(len(matched_parts))
            
            with live:
                for i, part in enumerate(matched_parts):
                    # Canonical key match
                    # To handle site-wide inconsistencies (where site system code differs from Excel),
                    # we compare primarily based on assembly and part name.
                    part_norm = self.matcher._normalize(part['part'])
                    asm_norm = self.matcher._normalize(part['assembly'])
                    comm_norm = self.matcher._normalize(part.get('comments', ''))

                    found_duplicate = False
                    for existing_key, existing_data in existing.items():
                        ex_part = self.matcher._normalize(existing_data.get('part', ''))
                        ex_asm = self.matcher._normalize(existing_data.get('assembly', ''))
                        ex_comm = self.matcher._normalize(existing_data.get('comments', ''))

                        if ex_part != part_norm:
                            continue
                        # If assembly is known on both sides, it must also match
                        if asm_norm and ex_asm and asm_norm != ex_asm:
                            continue
                        # Both part name and comments must match
                        if comm_norm != ex_comm:
                            continue
                        found_duplicate = True
                        self.ui.log(f"Row {part['row']}: Found duplicate match: Excel='{part['part']}' vs Site='{existing_data.get('part')}'", "SKIP")
                        break
                    
                    if found_duplicate:
                        status_table.add_row(str(part['row']), part['part'], "[blue]SKIP[/]", "Duplicate (already on site)")
                        self.ui.log(f"Row {part['row']}: Skipped duplicate '{part['part']}'", "SKIP")
                        self.ui.update_eta(progress, task_id, start_time, i + 1, len(matched_parts))
                        progress.update(task_id, advance=1)
                        self._smart_delay(self.config.base_delay)
                        continue

                    if self.config.dry_run:
                        status_table.add_row(str(part['row']), part['part'], "[magenta]DRY[/]", "Dry run - no upload")
                        self.ui.log(f"Row {part['row']}: Dry run - would upload '{part['part']}'", "DRY")
                    else:
                        try:
                            browser.create_part(part)
                            status_table.add_row(str(part['row']), part['part'], "[green]OK[/]", "Created")
                            self.ui.log(f"Row {part['row']}: Created '{part['part']}'", "OK")
                            existing[self.matcher.canonical_key(part['system'], part['assembly'], part['part'])] = part
                        except Exception as e:
                            status_table.add_row(str(part['row']), part['part'], "[red]ERR[/]", str(e))
                            self.ui.log(f"Row {part['row']}: Error creating '{part['part']}': {e}", "ERROR")

                    self.ui.update_eta(progress, task_id, start_time, i + 1, len(matched_parts))
                    progress.update(task_id, advance=1)
                    self._smart_delay(self.config.base_delay)

        self.ui.log("Automation finished.")
