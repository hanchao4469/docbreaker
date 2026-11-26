"""Expand table coordinates without rewriting or dropping the original table."""
from html import escape


def table_data(root):
    result = []
    for table in root.find_all("table", limit=24):
        rows = [row for row in table.find_all("tr", limit=64) if row.find_parent("table") is table]
        grid, headers = {}, []
        for i, row in enumerate(rows):
            col = 0
            cells = row.find_all(["td", "th"], recursive=False, limit=24)
            group = row.parent
            remaining = sum(r.parent is group for r in rows[i:]) if group.name in {"thead", "tbody", "tfoot"} else len(rows) - i
            if cells and (group.name == "thead" or all(c.name == "th" and c.get("scope") != "row" for c in cells)):
                headers.append(i)
            for cell in cells:
                while (i, col) in grid:
                    col += 1
                if col >= 24:
                    break
                raw = " ".join(" ".join(cell.stripped_strings).split())
                value = raw[:350] + ("…" if len(raw) > 350 else "")
                def span(name, maximum):
                    v = str(cell.get(name, "1"))
                    if v == "0" and name == "rowspan":
                        return maximum
                    return min(maximum, max(1, int(v))) if v.isascii() and v.isdigit() and len(v) < 5 else 1
                width = span("colspan", 24 - col)
                for rr in range(i, i + span("rowspan", remaining)):
                    for cc in range(col, col + width):
                        grid.setdefault((rr, cc), value)
                col += width
        if grid and len(rows) > 1:
            width = max(c for _, c in grid) + 1
            caption = table.find("caption", recursive=False) or table.find_previous(["h2", "h3", "h4", "h5"])
            result.append({"number": len(result) + 1, "caption": caption.get_text(" ", strip=True)[:140] if caption else "",
                           "headers": headers, "rows": [[grid.get((r, c), "") for c in range(width)] for r in range(len(rows))]})
    return result


def render_tables(tables, inferred):
    blocks, size = [], 0
    for table in tables:
        n, rows = table["number"], table["rows"]
        # Explicit source headers win; the model can select only existing early rows.
        headers = table["headers"] or inferred.get(n, [])
        lines = []
        title = f"Table {n}" + (" — " + escape(table["caption"]) if table["caption"] else "")
        labels = ["/".join(dict.fromkeys(rows[h][c] for h in headers if rows[h][c]))
                  for c in range(len(rows[0]))]
        for r, row in enumerate(rows):
            fields = [f"{escape(labels[c]) + ' ' if labels[c] and r not in headers else ''}[C{c + 1}]: {escape(value)}"
                      for c, value in enumerate(row) if value]
            if fields:
                line = f"- R{r + 1}: " + "；".join(fields)
                if size + len(line) > 16000:
                    break
                size += len(line)
                lines.append(line)
        if lines:
            blocks.append("### " + title + "\n\n" + "\n".join(lines))
        if size >= 16000:
            break
    return "\n\n".join(blocks)
