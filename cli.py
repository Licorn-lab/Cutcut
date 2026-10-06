"""Optional command-line entry, using the same engine as the GUI."""
import argparse
from core import Options, run_batch

def main():
    parser = argparse.ArgumentParser(description="Mac 本地批量抠图与白底输出")
    parser.add_argument("input", help="原图文件夹")
    parser.add_argument("output", help="输出文件夹")
    parser.add_argument("--model", choices=["u2net", "u2netp", "isnet-general-use", "framed-dual"], default="framed-dual", help="默认双模型装裱保护；普通物品可选 isnet-general-use")
    parser.add_argument("--size", type=int, default=1500)
    parser.add_argument("--occupancy", type=float, default=0.85)
    parser.add_argument("--original-size", action="store_true")
    parser.add_argument("--png", action="store_true", help="白底也使用 PNG")
    parser.add_argument("--jpg", action="store_true", help="使用 JPG 输出（默认，质量 100）")
    parser.add_argument("--square", action="store_true", help="统一为正方形；默认保留原图尺寸")
    parser.add_argument("--no-transparent", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--matting", action="store_true")
    args = parser.parse_args()
    options = Options(model=args.model, size=args.size, occupancy=args.occupancy,
                      layout="square" if args.square and not args.original_size else "original", white_format="png" if args.png else "jpg",
                      recursive=not args.no_recursive,
                      resume=not args.no_resume, alpha_matting=args.matting and args.model != "framed-dual")
    try:
        result = run_batch(args.input, args.output, options, emit=lambda event, value: print(value, flush=True)
                           if event in {"status", "log", "done"} else None)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"未完成：{exc}", flush=True)
        return 1
    return 1 if result["failed"] else 0

if __name__ == "__main__":
    raise SystemExit(main())
