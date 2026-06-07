import os
import re
import glob
import warnings
import pandas as pd
import openpyxl
from typing import List, Dict, Any, Optional, Tuple

class ExcelProcessor:
    SKIP_COLORS = {
        "FF00FF00", "0000FF00", "00FF00", "#00FF00",
        "FFFF0000", "00FF0000", "FF0000", "#FF0000",
    }
    GREEN_TARGET = (0, 255, 0)
    RED_TARGET = (255, 0, 0)
    COLOR_TOLERANCE = 110

    def __init__(self, boms_dir: str):
        self.boms_dir = boms_dir

    def discover_files(self) -> List[str]:
        search_dir = os.path.join(os.getcwd(), self.boms_dir)
        if not os.path.isdir(search_dir):
            os.makedirs(search_dir, exist_ok=True)
            return []
        return sorted(glob.glob(os.path.join(search_dir, "*.xlsx")))

    def _normalize_color(self, value: Any) -> Optional[str]:
        if not value:
            return None
        raw = str(value).strip()
        if raw.startswith("#"):
            raw = raw[1:]
        if raw.lower().startswith("0x"):
            raw = raw[2:]
        raw = raw.upper()
        if raw == "00000000":
            return None
        if len(raw) == 6:
            return raw
        if len(raw) == 8:
            return raw[2:]
        return None

    def _hex_to_rgb(self, hex_value: str) -> Optional[Tuple[int, int, int]]:
        if not hex_value or len(hex_value) != 6:
            return None
        try:
            return tuple(int(hex_value[i:i+2], 16) for i in (0, 2, 4))
        except ValueError:
            return None

    def _is_close_color(self, rgb: Tuple[int, int, int], target: Tuple[int, int, int]) -> bool:
        distance = sum((c - t) ** 2 for c, t in zip(rgb, target))
        return distance <= self.COLOR_TOLERANCE ** 2

    def _match_skip_color(self, raw: Any) -> Optional[str]:
        normalized = self._normalize_color(raw)
        if normalized in self.SKIP_COLORS:
            if normalized.endswith("00FF00"):
                return "green (done)"
            if normalized.endswith("FF0000"):
                return "red (skip)"

        rgb = self._hex_to_rgb(normalized)
        if rgb is None:
            return None

        if self._is_close_color(rgb, self.GREEN_TARGET):
            return "green (done)"
        if self._is_close_color(rgb, self.RED_TARGET):
            return "red (skip)"
        return None

    def get_cell_color(self, sheet, row: int, col: int = 1) -> Optional[str]:
        try:
            fill = sheet.cell(row=row, column=col).fill
            if not fill or not fill.patternType:
                return None
            color = fill.start_color or fill.fgColor
            if not color:
                return None
            
            raw = None
            if hasattr(color, "rgb") and color.rgb:
                raw = color.rgb
            elif hasattr(color, "index") and color.index:
                raw = color.index
            return self._normalize_color(raw)
        except Exception:
            # Return None if the color value can't be normalized
            return None

    def should_skip_row_color(self, sheet, row: int) -> Optional[str]:
        for col in range(1, min(sheet.max_column, 5) + 1):
            color = self.get_cell_color(sheet, row, col)
            skip = self._match_skip_color(color)
            if skip:
                return skip
        return None

    def process_file(self, filepath: str, run_system: str = "ALL", system_norm_map: dict = None, run_assembly: list = None) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        stats = {
            "total_excel_rows": 0,
            "empty_rows": 0,
            "example_rows": 0,
            "system_mismatch": 0,
            "assembly_mismatch": 0,
            "skipped_by_color": 0,
            "valid_parts": 0
        }
        
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
            wb = openpyxl.load_workbook(filepath, data_only=True)
        sheet = wb.active
        df = pd.read_excel(filepath)
        stats["total_excel_rows"] = len(df)
        
        raw_cols = [str(c).strip().lower() for c in df.columns]
        
        def find_col(aliases):
            for a in aliases:
                if a in raw_cols:
                    return raw_cols.index(a)
            return None

        col_map = {
            "system": find_col(["system", "sys"]),
            "assembly": find_col(["assembly", "asm", "assy"]),
            "subassembly": find_col(["subassembly", "sub assembly", "sub_assembly", "subasm"]),
            "part": find_col(["part", "part name", "designation"]),
            "quantity": find_col(["part_quantity", "quantity", "qty", "amount"]),
            "makebuy": find_col(["make o. buy", "m/b", "makebuy", "make/buy"]),
            "comments": find_col(["part_comments", "comments", "notes", "comment"]),
            # Sub-entry columns (Materials / Processes / Overhead rows in CCBOM)
            "entry_type": find_col(["type"]),
            "entry_subtype": find_col(["subtype"]),
            "entry_subtype_name": find_col(["subtype name"]),
            "entry_comments": find_col(["comment process", "comments (process)"]),
            "entry_quantity": find_col(["quantity 2", "quantity2", "qty2"]),
            "entry_cost": find_col(["cost", "costs"]),
            "entry_cost_comments": find_col(["comments costs", "comments (costs)"]),
            "entry_emissions": find_col(["emissions"]),
            "entry_emissions_comments": find_col(["comments emissions", "comments (emissions)"]),
        }

        if col_map["system"] is None or col_map["part"] is None:
            raise ValueError(f"Could not identify required 'system' or 'part' columns in {filepath}")

        filtered = []
        for idx, row in df.iterrows():
            excel_row = idx + 2

            # Extract part and type values first for sub-entry detection
            part_val = str(row.iloc[col_map["part"]] if col_map["part"] is not None else "").strip()
            part_val = "" if part_val.lower() in ("nan", "0") else part_val

            type_raw = str(row.iloc[col_map["entry_type"]] if col_map["entry_type"] is not None else "").strip()
            type_val = "" if type_raw.lower() == "nan" else type_raw

            # Sub-entry row: any row with a Type value (Material / Process / Overhead)
            # is a sub-entry. This works regardless of whether the system/assembly
            # columns are filled in (many sheets repeat the system for all rows).
            if type_val and filtered:
                if not self.should_skip_row_color(sheet, excel_row):
                    def _get(key, r=row):
                        c = col_map.get(key)
                        if c is None:
                            return ""
                        v = str(r.iloc[c]).strip()
                        return "" if v.lower() == "nan" else v
                    filtered[-1]["sub_entries"].append({
                        "type": type_val,
                        "subtype": _get("entry_subtype"),
                        "subtype_name": _get("entry_subtype_name"),
                        "comments": _get("entry_comments"),
                        "quantity": _get("entry_quantity"),
                        "cost": _get("entry_cost"),
                        "cost_comments": _get("entry_cost_comments"),
                        "emissions": _get("entry_emissions"),
                        "emissions_comments": _get("entry_emissions_comments"),
                    })
                continue

            # Regular part row processing
            sys_raw = str(row.iloc[col_map["system"]] if col_map["system"] is not None else "").strip()
            if system_norm_map:
                norm = re.sub(r"\W+", "", sys_raw.lower())
                sys_val = system_norm_map.get(norm, sys_raw).upper()
            else:
                sys_val = sys_raw.upper()

            # Skip rows with empty/invalid values
            if not sys_val or sys_val == "NAN" or not part_val:
                stats["empty_rows"] += 1
                continue

            # Skip rows whose system didn't resolve to a known 2-char code
            if system_norm_map and sys_val not in system_norm_map.values():
                stats["empty_rows"] += 1
                continue

            if any(x in sys_val for x in ["BEISPIEL", "EXAMPLE"]) or \
               any(x in part_val.upper() for x in ["BEISPIEL", "EXAMPLE"]):
                stats["example_rows"] += 1
                continue

            if run_system != "ALL" and sys_val != run_system:
                stats["system_mismatch"] += 1
                continue

            if self.should_skip_row_color(sheet, excel_row):
                stats["skipped_by_color"] += 1
                continue

            asm_val = str(row.iloc[col_map["assembly"]] if col_map["assembly"] is not None else "").strip()

            if run_assembly and asm_val.lower() not in {a.lower() for a in run_assembly}:
                stats["assembly_mismatch"] += 1
                continue
            subasm_val = str(row.iloc[col_map["subassembly"]] if col_map["subassembly"] is not None else "").strip().replace("nan", "")
            qty_val = str(row.iloc[col_map["quantity"]] if col_map["quantity"] is not None else "").strip()
            mb_val = str(row.iloc[col_map["makebuy"]] if col_map["makebuy"] is not None else "m").strip().lower()[:1] or "m"
            comm_val = str(row.iloc[col_map["comments"]] if col_map["comments"] is not None else "").strip().replace("nan", "")

            filtered.append({
                "row": excel_row,
                "system": sys_val,
                "assembly": asm_val,
                "subassembly": subasm_val,
                "part": part_val,
                "makebuy": mb_val,
                "quantity": qty_val,
                "comments": comm_val,
                "sub_entries": [],
            })
            stats["valid_parts"] += 1

        return filtered, stats
