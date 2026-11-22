"""Local image viewers: drawing and adapters for COCO/Supervisely annotations."""
from __future__ import annotations

import json
import math
import random
import textwrap
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import to_rgba
from matplotlib.patches import PathPatch, Rectangle
from matplotlib.path import Path as PlotPath
from PIL import Image, ImageOps

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".avif"}


def project_root():
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "datasets").is_dir():
            return candidate
    raise FileNotFoundError("Откройте ноутбук из папки проекта, содержащей datasets/.")


def image_files(directory):
    return sorted(path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)


def read_jsonl(path):
    """Read a live parser export; tolerate only a torn final record."""
    if not path.exists():
        return []
    records = []
    with path.open("rb") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except (ValueError, UnicodeDecodeError):
                if not line.endswith(b"\n") and stream.read(1) == b"":
                    break
                raise
    return records


def load_coco(annotation_path, image_dir, split, fallback_dir=None):
    with annotation_path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    categories = {item["id"]: item["name"] for item in data["categories"]}
    grouped = defaultdict(list)
    for item in data["annotations"]:
        segmentation = item.get("segmentation", [])
        if not isinstance(segmentation, list):
            raise ValueError(f"{annotation_path}: ожидаются COCO-полигоны, получена RLE-разметка.")
        polygons = []
        for polygon in segmentation:
            if len(polygon) >= 6 and len(polygon) % 2 == 0:
                polygons.append(np.asarray(polygon, dtype=float).reshape(-1, 2).tolist())
        grouped[item["image_id"]].append({
            "label": categories[item["category_id"]], "class_id": item["category_id"],
            "polygons": polygons, "bbox": item.get("bbox"),
            "annotation_source": annotation_path.name, "raw": item,
        })
    samples = []
    for info in data["images"]:
        path = image_dir / info["file_name"]
        if not path.is_file() and fallback_dir is not None:
            path = fallback_dir / info["file_name"]
        samples.append({
            "image_path": path, "file_name": info["file_name"], "split": split,
            "width": info.get("width"), "height": info.get("height"),
            "annotations": grouped[info["id"]], "metadata": info, "task": "segmentation",
        })
    return samples


def load_supervisely(directory):
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    classes = {item["id"]: item for item in meta["classes"]}
    samples = []
    for annotation_path in sorted((directory / "File1" / "ann").glob("*.json")):
        data = json.loads(annotation_path.read_text(encoding="utf-8"))
        annotations = []
        for obj in data.get("objects", []):
            if obj.get("geometryType") != "polygon":
                raise ValueError(f"Неизвестный тип геометрии в {annotation_path}: {obj.get('geometryType')}")
            class_info = classes.get(obj.get("classId"), {})
            annotations.append({
                "label": obj.get("classTitle") or class_info.get("title", "unknown"),
                "class_id": obj.get("classId"), "color": class_info.get("color"),
                "polygons": [obj["points"]["exterior"]],
                "holes": obj["points"].get("interior", []), "raw": obj,
            })
        filename = annotation_path.name.removesuffix(".json")
        samples.append({
            "image_path": directory / "File1" / "img" / filename,
            "file_name": filename, "split": "all",
            "width": data.get("size", {}).get("width"),
            "height": data.get("size", {}).get("height"),
            "annotations": annotations, "metadata": data, "task": "segmentation",
        })
    return samples, meta


def summarize(samples):
    splits = Counter(sample.get("split", "all") for sample in samples)
    classes = Counter()
    missing = 0
    for sample in samples:
        if not Path(sample["image_path"]).is_file():
            missing += 1
        if sample.get("label") is not None:
            classes[str(sample["label"])] += 1
        classes.update(annotation["label"] for annotation in sample.get("annotations", []))
    print(f"Записей: {len(samples):,}; изображения отсутствуют: {missing:,}")
    print("Выборки:", dict(splits))
    if classes:
        print("Классы (число меток):")
        for label, count in sorted(classes.items()):
            print(f"  {label}: {count:,}")
    return {"records": len(samples), "missing_images": missing, "splits": dict(splits), "classes": dict(classes)}


def _ring(points, hole=False):
    points = np.asarray(points, dtype=float)
    if len(points) > 1 and np.array_equal(points[0], points[-1]):
        points = points[:-1]
    if len(points) < 3:
        return None
    signed_area = np.sum(points[:, 0] * np.roll(points[:, 1], -1) - np.roll(points[:, 0], -1) * points[:, 1])
    if (signed_area > 0) == hole:
        points = points[::-1]
    vertices = np.vstack([points, points[0]])
    codes = [PlotPath.MOVETO] + [PlotPath.LINETO] * (len(points) - 1) + [PlotPath.CLOSEPOLY]
    return PlotPath(vertices, codes)


def _polygon_patch(points, holes, scale, color):
    exterior = _ring(np.asarray(points, dtype=float) * scale)
    if exterior is None:
        return None
    rings = [exterior]
    for hole in holes:
        ring = _ring(np.asarray(hole, dtype=float) * scale, hole=True)
        if ring is not None:
            rings.append(ring)
    return PathPatch(PlotPath.make_compound_path(*rings), facecolor=to_rgba(color, 0.22), edgecolor=color, linewidth=1.4)


def _bbox(annotation):
    if annotation.get("bbox") is not None:
        return annotation["bbox"]
    points = [point for polygon in annotation.get("polygons", []) for point in polygon]
    if not points:
        return None
    values = np.asarray(points, dtype=float)
    x, y = values.min(axis=0)
    right, bottom = values.max(axis=0)
    return [x, y, right - x, bottom - y]


def _label_position(x, y, label, width, height, occupied):
    # Reserve approximate label rectangles in image coordinates to reduce
    # collisions on photographs with many neighbouring objects.
    box_width = min(width * 0.95, width * (0.011 * len(label) + 0.025))
    box_height = height * 0.042
    x = min(max(0, x), max(0, width - box_width))
    y = min(max(0, y), max(0, height - box_height))
    for offset in [0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5]:
        top = min(max(0, y + offset * box_height * 1.1), max(0, height - box_height))
        rect = (x, top, x + box_width, top + box_height)
        if all(rect[2] <= other[0] or other[2] <= rect[0] or rect[3] <= other[1] or other[3] <= rect[1] for other in occupied):
            occupied.append(rect)
            return x, top
    occupied.append((x, y, x + box_width, y + box_height))
    return x, y


def show_samples(
    samples, n_images=6, n_cols=3, seed=None, split="all", class_filter=None,
    show_masks=True, show_boxes=True, show_labels=True, show_class_ids=False,
    only_annotated=False, photo_index=None,
):
    """Draw random local images with native class names. Missing photos use a labelled canvas."""
    if n_images < 1 or n_cols < 1:
        raise ValueError("N_IMAGES и N_COLS должны быть положительными.")
    if photo_index is not None and photo_index < 0:
        raise ValueError("PHOTO_INDEX должен быть None или неотрицательным числом.")
    available_splits = {sample.get("split", "all") for sample in samples}
    if split != "all" and split not in available_splits:
        raise ValueError(f"Нет выборки {split!r}. Доступны: {sorted(available_splits)}")
    wanted = {str(class_filter)} if isinstance(class_filter, (str, int)) else set(map(str, class_filter or []))
    candidates = []
    for sample in samples:
        annotations = sample.get("annotations", [])
        labels = {str(annotation["label"]) for annotation in annotations}
        if sample.get("label") is not None:
            labels.add(str(sample["label"]))
        if split != "all" and sample.get("split") != split:
            continue
        if wanted and not labels.intersection(wanted):
            continue
        if only_annotated and not annotations and sample.get("label") is None:
            continue
        candidates.append(sample)
    if not candidates:
        raise ValueError("Нет записей для выбранных SPLIT / CLASS_FILTER / ONLY_ANNOTATED.")
    rng = random.Random(seed)
    selected = [dict(sample) for sample in rng.sample(candidates, min(n_images, len(candidates)))]
    all_labels = sorted({annotation["label"] for sample in samples for annotation in sample.get("annotations", [])})
    palette = plt.get_cmap("tab20")
    colors = {label: palette(index % 20) for index, label in enumerate(all_labels)}
    colors["damage"] = "#ed303b"
    cols = min(n_cols, len(selected))
    rows = math.ceil(len(selected) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(5.4 * cols, 4.9 * rows), squeeze=False)

    for ax, sample in zip(axes.flat, selected):
        if sample.get("photo_paths"):
            paths = sample["photo_paths"]
            sample["image_path"] = rng.choice(paths) if photo_index is None else paths[min(photo_index, len(paths) - 1)]
            sample["file_name"] = Path(sample["image_path"]).name
        annotations = sample.get("annotations", [])
        missing = None
        try:
            with Image.open(sample["image_path"]) as source:
                image = source.convert("RGB") if annotations else ImageOps.exif_transpose(source).convert("RGB")
        except (OSError, ValueError) as exc:
            missing = str(exc)
            image = Image.new("RGB", (int(sample.get("width") or 640), int(sample.get("height") or 480)), "#eeeeee")
        width, height = image.size
        scale = np.array([width / (sample.get("width") or width), height / (sample.get("height") or height)])
        ax.imshow(image, extent=(0, width, height, 0))
        occupied = []
        if missing:
            ax.text(0.5, 0.98, "Изображение отсутствует\nПоказана только разметка", transform=ax.transAxes,
                    ha="center", va="top", color="#333333", fontsize=9,
                    bbox={"facecolor": "white", "alpha": 0.9, "edgecolor": "none"}, zorder=10)
        for annotation in annotations:
            color = annotation.get("color") or colors[annotation["label"]]
            if show_masks:
                for index, polygon in enumerate(annotation.get("polygons", [])):
                    patch = _polygon_patch(polygon, annotation.get("holes", []) if index == 0 else [], scale, color)
                    if patch is not None:
                        ax.add_patch(patch)
            box = _bbox(annotation)
            if box is None:
                continue
            x, y, box_width, box_height = np.asarray(box, dtype=float) * np.tile(scale, 2)
            if show_boxes:
                ax.add_patch(Rectangle((x, y), box_width, box_height, fill=False, edgecolor=color, linewidth=1.2, linestyle="--"))
            if show_labels:
                label = annotation["label"]
                if show_class_ids and annotation.get("class_id") is not None:
                    label += f" (id={annotation['class_id']})"
                lx, ly = _label_position(x, y, label, width, height, occupied)
                if (lx, ly) != (x, y):
                    ax.plot([x, lx], [y, ly], color=color, linewidth=0.65, alpha=0.8)
                ax.text(lx, ly, label, color="white", fontsize=7, va="top",
                        bbox={"facecolor": color, "edgecolor": "none", "alpha": 0.9, "pad": 1.5})
        filename = sample.get("file_name") or Path(sample["image_path"]).name
        title = textwrap.fill(f"{sample.get('split', '')} / {filename}", 48)
        if sample.get("task") == "classification":
            label = sample.get("label")
            label_text = str(label) if label is not None else "отсутствует в разметке"
            if label is not None and show_class_ids and sample.get("class_id") is not None:
                label_text += f" (id={sample['class_id']})"
            title += "\nКласс: " + label_text
        elif sample.get("task") == "listing":
            title = "\n".join(textwrap.fill(line, 48) for line in (sample.get("caption") or filename).splitlines())
        else:
            title += f"\nОбъектов: {len(annotations)}"
        ax.set_title(title, fontsize=9)
        ax.set_xlim(0, width)
        ax.set_ylim(height, 0)
        ax.axis("off")
        sample["image_missing"] = missing is not None
    for ax in list(axes.flat)[len(selected):]:
        ax.axis("off")
    fig.tight_layout()
    plt.show()
    return fig, selected
