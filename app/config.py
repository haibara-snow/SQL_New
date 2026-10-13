from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "sites.json"


@dataclass(frozen=True)
class Site:
    site_id: str
    branch_id: int
    url: str
    host: str
    port: int
    database: Path


@dataclass(frozen=True)
class Settings:
    main_host: str
    main_port: int
    request_timeout_seconds: float
    query_timeout_seconds: float
    max_rows_per_query: int
    sites: tuple[Site, ...]
    root: Path
    main_url: str = "http://127.0.0.1:8000"

    def site(self, site_id: str) -> Site:
        for site in self.sites:
            if site.site_id == site_id:
                return site
        raise KeyError(f"Unknown site: {site_id}")


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.environ.get("DISTLIB_CONFIG", DEFAULT_CONFIG)).resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    root = config_path.parent.parent
    main = raw["main"]
    sites = tuple(
        Site(
            site_id=str(item["site_id"]),
            branch_id=int(item["branch_id"]),
            url=str(item["url"]).rstrip("/"),
            host=str(item["host"]),
            port=int(item["port"]),
            database=(root / item["database"]).resolve(),
        )
        for item in raw["sites"]
    )
    if len(sites) < 3:
        raise ValueError("The distribution directory must contain at least three sites")
    if len({site.branch_id for site in sites}) != len(sites):
        raise ValueError("Each site must have a unique branch_id")
    if len({site.site_id for site in sites}) != len(sites):
        raise ValueError("Each site must have a unique site_id")
    return Settings(
        main_host=str(main["host"]),
        main_port=int(main["port"]),
        request_timeout_seconds=float(main.get("request_timeout_seconds", 8)),
        query_timeout_seconds=float(main.get("query_timeout_seconds", 5)),
        max_rows_per_query=int(main.get("max_rows_per_query", 10000)),
        sites=sites,
        root=root,
        main_url=str(main.get("url", f"http://{main['host']}:{main['port']}")).rstrip("/"),
    )
