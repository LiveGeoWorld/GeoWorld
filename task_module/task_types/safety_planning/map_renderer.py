"""Source-bound private maps. Geometry is never interpreted as event exposure."""

import hashlib
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .map_scene import build_scene, payload_hash


class SafetyMapRenderer:
    def __init__(self, output_directory):
        self.output_directory = Path(output_directory)

    def __call__(self, context):
        objects, missing = build_scene(context)
        if not objects:
            raise ValueError("No cached geometry to render; cannot fabricate a Safety map")
        try:
            font = ImageFont.truetype("arial.ttf", 14)
        except OSError:
            font = ImageFont.load_default()
        overview = [obj for obj in objects if obj["kind"] != "step"] or objects
        pages = [("Overview", overview)]
        steps = [obj for obj in objects if obj["kind"] == "step"]
        if steps:
            pages.append(("Road sections", steps))
        if context.get("protection_contract") in {"safety_occupancy.v1", "safety_occupancy.v2", "safety_occupancy.v3", "safety_occupancy.v4", "safety_occupancy.v5", "safety_occupancy.v6"}:
            pages = [("Task routes and coloring", objects)]
        digest = payload_hash(context)
        self.output_directory.mkdir(parents=True, exist_ok=True)
        results, represented = [], set()
        for title, focus in pages:
            results.append(self._page(title, focus, objects, missing, digest, len(results), font, represented))
        for obj in objects:
            if obj["kind"] == "coloring" and obj["label"] not in represented:
                results.append(self._page("Coloring detail", [obj], objects, [], digest, len(results), font, represented))
                if obj["label"] not in represented:
                    raise ValueError("Protected geometry is not readable in attached maps")
        # Short/crowded sections keep their exact text geometry rather than adding
        # many images that cost more than the small remaining coordinate payload.
        return results

    def _page(self, title, focus, objects, missing, digest, number, font, represented):
        coords = [p for obj in focus for path in obj["paths"] for p in path]
        coords += [p for obj in focus for p in obj.get("endpoints", [])]
        x0, x1 = min(p[0] for p in coords), max(p[0] for p in coords)
        y0, y1 = min(p[1] for p in coords), max(p[1] for p in coords)
        cosine = max(.01, math.cos(math.radians((y0 + y1) / 2)))
        scale = min(600 / max((x1 - x0) * cosine, .00001), 320 / max(y1 - y0, .00001))

        def project(p):
            return (380 + (p[0] - (x0 + x1) / 2) * cosine * scale,
                    215 - (p[1] - (y0 + y1) / 2) * scale)

        bitmap = Image.new("RGB", (800, 560), "#fafafa")
        draw = ImageDraw.Draw(bitmap)
        draw.text((20, 12), f"{title} | north up | cached geometry, no basemap", font=font, fill="#222222")
        # Context is clipped, and never counts as covered evidence on this page.
        backdrop = Image.new("RGB", (800, 420), "#fafafa")
        bg = ImageDraw.Draw(backdrop)
        for obj in objects:
            if obj["kind"] == "route":
                for path in obj["paths"]:
                    bg.line([project(p) for p in path], fill="#dddddd", width=2)
        bitmap.paste(backdrop.crop((0, 35, 800, 420)), (0, 35))
        edges = {}
        for obj in focus:
            if obj["kind"] == "coloring":
                continue
            for path in obj["paths"]:
                for a, b in zip(path, path[1:]):
                    edges.setdefault(tuple(sorted((a, b))), []).append(obj)
        shared = []
        for (a, b), owners in edges.items():
            colors = list(dict.fromkeys(obj["color"] for obj in owners))
            pa, pb = project(a), project(b)
            n = max(1, math.ceil(math.dist(pa, pb) / 5))
            for i in range(n):
                segment = [tuple(pa[k] + (pb[k] - pa[k]) * t / n for k in (0, 1)) for t in (i, i + 1)]
                draw.line(segment, fill=colors[i % len(colors)], width=4)
            if len(colors) > 1:
                labels = sorted({obj["label"] for obj in owners})
                if labels not in shared:
                    shared.append(labels)
        layer = Image.new("RGBA", bitmap.size)
        colored = ImageDraw.Draw(layer)
        for obj in focus:
            if obj["kind"] != "coloring":
                continue
            from .occupancy import geometry
            shape = geometry(obj["geometry"])
            if shape.geom_type in {"Polygon", "MultiPolygon"}:
                mask = Image.new("L", bitmap.size, 0)
                mask_draw = ImageDraw.Draw(mask)
                polygons = [shape] if shape.geom_type == "Polygon" else shape.geoms
                for polygon in polygons:
                    mask_draw.polygon([project(p) for p in polygon.exterior.coords], fill=70)
                    for ring in polygon.interiors:
                        mask_draw.polygon([project(p) for p in ring.coords], fill=0)
                fill = Image.new("RGBA", bitmap.size, (132, 119, 143, 0))
                fill.putalpha(mask)
                layer = Image.alpha_composite(layer, fill)
                colored = ImageDraw.Draw(layer)
                for path in obj["paths"]:
                    colored.line([project(p) for p in path], fill=(132, 119, 143, 190), width=1)
                continue
            for path in obj["paths"]:
                pixels = [project(p) for p in path]
                if len(path) == 1:
                    x, y = pixels[0]
                    colored.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(132, 119, 143, 190))
                else:
                    for a, b in zip(path, path[1:]):
                        pa, pb = project(a), project(b)
                        owners = edges.get(tuple(sorted((a, b))), [])
                        shift = 3 if owners else 0
                        length = max(math.dist(pa, pb), 1)
                        dx, dy = -(pb[1] - pa[1]) / length * shift, (pb[0] - pa[0]) / length * shift
                        qa, qb = (pa[0] + dx, pa[1] + dy), (pb[0] + dx, pb[1] + dy)
                        colored.line((qa, qb), fill=(132, 119, 143, 190), width=3)
                        if shift:
                            colored.line((pa, qa), fill=(132, 119, 143, 190))
                            colored.line((pb, qb), fill=(132, 119, 143, 190))
        bitmap = Image.alpha_composite(bitmap.convert("RGBA"), layer).convert("RGB")
        draw = ImageDraw.Draw(bitmap)
        occupied, legend, coverage, labels_to_draw = [], [], [], []
        for obj in focus:
            for index, p in enumerate(obj.get("endpoints", [])):
                px, py = project(p)
                bounds = (px - 3, py - 3, px + 3, py + 3)
                (draw.rectangle if index == 0 else draw.ellipse)(bounds, fill=obj["color"])
        for obj in focus:
            pixels = [project(p) for path in obj["paths"] for p in path]
            x, y = pixels[len(pixels) // 2]
            if obj["kind"] == "place":
                draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=obj["color"])
            label = obj["label"]
            box = draw.textbbox((0, 0), label, font=font)
            width, height = box[2] + 10, 23
            placed = False
            for radius in (12, 35, 65, 100, 150):
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1)):
                    lx = min(790 - width, max(10, x + dx * radius))
                    ly = min(410 - height, max(40, y + dy * radius))
                    rect = (lx, ly, lx + width, ly + height)
                    if any(rect[0] < b[2] and rect[2] > b[0] and rect[1] < b[3] and rect[3] > b[1] for b in occupied):
                        continue
                    draw.line((x, y, lx + width / 2, ly + height / 2), fill="#888888")
                    labels_to_draw.append((rect, label, obj["color"]))
                    occupied.append(rect)
                    placed = True
                    break
                if placed:
                    break
            span = max(max(p[k] for p in pixels) - min(p[k] for p in pixels) for k in (0, 1))
            legible = placed and (obj["kind"] == "place" or all(len(p) == 1 for p in obj["paths"]) or span >= 10)
            if legible:
                represented.add(label)
                coverage.extend({**entry, "label": label} for entry in obj["coverage"])
            keys = ("label", "source_ref", "route_label") if obj["kind"] == "step" else ("label", "kind", "source_ref", "owners", "candidate_ref", "candidate_label", "color")
            legend.append({key: obj[key] for key in keys if obj.get(key) is not None})
            if not legible:
                legend[-1]["geometry_in_text"] = True
        for rect, label, color in labels_to_draw:
            draw.rectangle(rect, fill="white", outline=color)
            draw.text((rect[0] + 5, rect[1] + 2), label, font=font, fill=color)
        for i, obj in enumerate(focus[:6]):
            text = f'{obj["label"]}: {obj.get("candidate_ref") or obj["kind"]}'
            if obj.get("is_gt"):
                text += " (GT)"
            draw.text((20 + (i % 2) * 390, 430 + (i // 2) * 25), text, font=font, fill=obj["color"])
        draw.text((20, 516), "Shared edges: alternating colors. Section start: square; end: circle.", font=font, fill="#444444")
        draw.text((20, 537), "Full source bindings in text legend. No inferred widths or area boundaries.", font=font, fill="#444444")
        image_ref = f"safety_map_v2_{digest[:12]}_{number + 1}"
        path = self.output_directory / f"{image_ref}.png"
        bitmap.save(path)
        return {"image_ref": image_ref, "path": str(path.resolve()),
                "legend": {"layers": legend, "not_drawn": missing if number == 0 else [], "shared_edges": shared,
                           "projection": "local equirectangular; no inferred boundaries"},
                "source_refs": list(dict.fromkeys(obj["source_ref"] for obj in focus)),
                "geometry_coverage": coverage, "payload_sha256": digest,
                "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
