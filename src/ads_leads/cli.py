"""CLI: ads-leads web | init-db."""

import argparse
import socket
import webbrowser

from ads_leads.config import get_settings


def _port_busy(host: str, port: int) -> bool:
    probe = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((probe, port)) == 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ads-leads")
    sub = parser.add_subparsers(dest="command", required=True)

    web = sub.add_parser("web", help="запустить веб-интерфейс")
    web.add_argument("--host", default=None)
    web.add_argument("--port", type=int, default=None)
    web.add_argument("--open", action="store_true", help="открыть браузер")

    sub.add_parser("init-db", help="создать таблицы БД")

    sq = sub.add_parser("suggest-queries", help="подобрать коммерческие запросы ниши из базы Keys.so")
    sq.add_argument("niche", help="название ниши, например «сантехника» (для известных ниш есть готовые семена)")
    sq.add_argument("--seeds", help="свои семена через запятую: «смеситель,унитаз,grohe»")
    sq.add_argument("--regions", default="msk", help="базы Keys.so через запятую (для отсева чужих городов)")
    sq.add_argument("--per-seed", type=int, default=20)
    sq.add_argument("--out", default=None, help="файл .tsv (по умолчанию data/queries_<ниша>.tsv)")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "suggest-queries":
        from ads_leads.keyso import KeysoClient
        from ads_leads.keywords import preset_seeds, suggest_queries
        from ads_leads.regions import parse_regions

        seeds = [s.strip() for s in args.seeds.split(",")] if args.seeds else preset_seeds(args.niche)
        regions = parse_regions(args.regions) or ["msk"]
        out = args.out or str(settings.data_dir / f"queries_{args.niche.strip().lower().replace(' ', '_')}.tsv")
        print(f"Семян: {len(seeds)} (это {len(seeds)} из 100 запросов к базе Keys.so в сутки)")
        with KeysoClient(settings.keyso_api_token) as client:
            res = suggest_queries(client, seeds, regions, per_seed=args.per_seed,
                                  on_progress=lambda p, s: print(f"  {p:3d}%  {s}"))
        with open(out, "w", encoding="utf-8") as f:
            f.write("запрос\tчастотность (!точная, мес)\tгруппа\n")
            f.writelines(f"{s.text}\t{s.freq}\t{s.seed}\n" for s in res)
        print(f"Готово: {len(res)} запросов → {out}")
    elif args.command == "init-db":
        from ads_leads.db import init_db

        init_db()
        print(f"БД готова: {settings.db_url}")
    elif args.command == "web":
        import uvicorn

        host = args.host or settings.host
        port = args.port or settings.port
        if _port_busy(host, port):
            print(
                f"\nПорт {port} уже занят: скорее всего, AdBeam Leads уже запущен в другом окне.\n"
                "Закройте все чёрные окна AdBeam Leads и запустите run.bat ещё раз."
            )
            raise SystemExit(1)
        if args.open:
            webbrowser.open(f"http://127.0.0.1:{port}")
        uvicorn.run("ads_leads.web.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
