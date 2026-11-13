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

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "init-db":
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
