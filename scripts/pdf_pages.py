import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from common import project_path, read_json, write_text


def selected_pages(specification, total, maximum=12):
    result = set()
    for segment in specification.split(","):
        parts = segment.strip().split("-")
        if len(parts) == 1:
            first = last = int(parts[0])
        elif len(parts) == 2:
            first, last = map(int, parts)
        else:
            raise ValueError("页序格式应为 6-10 或 6,8-10")
        if first < 1 or last < first or last > total:
            raise ValueError("PDF 页序超出文件范围，或范围倒置")
        if last - first + 1 > maximum:
            raise ValueError("请求超过单批准备上限，请拆分页段；整项任务可包含多批")
        result.update(range(first, last + 1))
        if len(result) > maximum:
            raise ValueError("请求超过单批准备上限，请拆分页段；整项任务可包含多批")
    if not result:
        raise ValueError("必须明确页序范围")
    return sorted(result)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="准备一个明确 PDF 页段；可作为批量任务的一批，不调用 OCR 或部署算力")
    parser.add_argument("--root", default=".")
    parser.add_argument("--source", required=True)
    parser.add_argument("--pages", required=True)
    parser.add_argument("--max-pages", type=int, default=12, help="单批准备上限，不是整项任务的总页数上限")
    parser.add_argument("--renderer", default="pdftoppm")
    parser.add_argument("--scale", type=int, default=1800)
    parser.add_argument("--extract-text", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    catalogue = read_json(root, "materials/catalogue.json")
    source = next((item for item in catalogue["sources"] if item["id"] == args.source), None)
    if source is None:
        parser.error("资料 ID 不存在")
    try:
        pdf = project_path(root, source["path"])
        digest = file_digest(pdf)
        if source.get("sha256") and digest != source["sha256"]:
            raise ValueError("资料指纹已改变，先更新版本、目录与页码映射，不能复用旧缓存")
        pages = selected_pages(args.pages, source["page_count"], args.max_pages)
        renderer = shutil.which(args.renderer)
        if renderer is None:
            raise ValueError("缺少 pdftoppm；先定位可用 PDF 渲染工具，再用 --renderer 指定实际路径")
        output = project_path(root, f"materials/fragments/{args.source}/{digest[:12]}/pdf-" + "_".join(str(p) for p in pages))
        output.mkdir(parents=True, exist_ok=True)
        manifest = {"source_id": args.source, "sha256": digest, "pdf_pages": pages,
                    "state": "prepared_not_verified", "ocr": "not_run", "pages": []}
        reader = None
        if args.extract_text:
            try:
                from pypdf import PdfReader
            except ImportError as error:
                raise ValueError("文本提取需要 pypdf，请使用已配置的 PDF 运行环境") from error
            reader = PdfReader(pdf)
        for page in pages:
            prefix = output / f"pdf-{page:04d}"
            png = prefix.with_suffix(".png")
            if not png.exists():
                subprocess.run([renderer, "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", str(args.scale), "-png", str(pdf), str(prefix)], check=True, capture_output=True)
            item = {"pdf_page": page, "image": png.name, "verification": "pending"}
            if reader is not None:
                text = reader.pages[page - 1].extract_text() or ""
                write_text(prefix.with_suffix(".txt"), text)
                item["text_characters"] = len(text)
                item["text_state"] = "extracted_not_verified" if text.strip() else "no_text_layer_detected"
            manifest["pages"].append(item)
        write_text(output / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"directory": str(output), "pdf_pages": pages, "state": manifest["state"], "ocr": manifest["ocr"]}, ensure_ascii=False, indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
