from __future__ import annotations

import argparse
import json
import webbrowser
from pathlib import Path

from .core import DEFAULT_WATCHES, Monitor, Settings
from .server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Riven Sniper : cinq armes PC, alertes locales et Discord.")
    parser.add_argument("--demo", action="store_true", help="Flux simulé, sans appel à warframe.market.")
    parser.add_argument("--open", action="store_true", help="Ouvrir le tableau de bord dans le navigateur local.")
    parser.add_argument("--diagnose", action="store_true", help="Une tentative HTTPS, rapport diagnostic sans identifiants, puis arrêt.")
    parser.add_argument("--interval", type=float, default=1.0, help="Secondes entre deux débuts de requête (minimum 1).")
    parser.add_argument("--collection", choices=["hybrid", "recent", "weapons"], default="hybrid",
                        help="hybrid : récents + recherches par arme ; recent : ancien flux seul ; weapons : armes seules.")
    parser.add_argument("--weapon", choices=[w.weapon for w in DEFAULT_WATCHES], help="Surveiller une seule arme plutôt que les cinq.")
    parser.add_argument("--min-price", type=int, help="Seuil personnalisé en mode une arme.")
    parser.add_argument("--max-price", type=int, help="Seuil personnalisé en mode une arme.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path(".riven-sniper"))
    args = parser.parse_args()
    try:
        if args.weapon or args.min_price is not None or args.max_price is not None:
            watch = next(w for w in DEFAULT_WATCHES if w.weapon == (args.weapon or "dual_toxocyst"))
            settings = Settings.single(weapon=watch.weapon, min_price=watch.min_price if args.min_price is None else args.min_price,
                                       max_price=watch.max_price if args.max_price is None else args.max_price,
                                       interval_seconds=args.interval, collection_mode=args.collection)
        else:
            settings = Settings(interval_seconds=args.interval, collection_mode=args.collection)
    except ValueError as exc:
        parser.error(str(exc))
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if args.diagnose:
        from .diagnostics import connection_report
        report = connection_report(settings)
        output = json.dumps(report, indent=2, ensure_ascii=False)
        (args.data_dir / "diagnostic.json").write_text(output, encoding="utf-8")
        print(output, flush=True)
        print("Rapport enregistré dans diagnostic.json, dans le dossier de données.", flush=True)
        raise SystemExit(0 if report["ok"] else 1)
    monitor = Monitor(settings, args.data_dir / ("demo.sqlite3" if args.demo else "live.sqlite3"), demo=args.demo,
                      discord_config_path=args.data_dir / ("discord-demo.json" if args.demo else "discord.json"))
    try:
        server = create_server(monitor, args.port)
    except OSError as exc:
        monitor.close()
        parser.error(f"Port indisponible ({exc}). Fermer l'autre instance ou choisir --port.")
    print(f"Riven Sniper — {'SIMULATION' if args.demo else 'API réelle'} — " + ", ".join(f"{w.label} {w.min_price}–{w.max_price} pl" for w in settings.watches), flush=True)
    print(f"Tableau local sur le port {server.server_port}. Arrêt : Ctrl+C. Ouvrir avec --open sur votre PC.", flush=True)
    monitor.start()
    if args.open:
        webbrowser.open(f"http://127.0.0.1:{server.server_port}")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        monitor.close()


if __name__ == "__main__":
    main()
