"""atomizer —— 供给原子化管线（开放数据 → data/atoms/*.json）

管线顺序（从仓库根目录运行）：
    python3 -m packages.atomizer.fetch     # OSM Overpass + Wikivoyage → data/raw/ → data/interim/candidates_*.json
    python3 -m packages.atomizer.cluster   # 地理聚类 → data/interim/clustered_*.json
    python3 -m packages.atomizer.enrich    # 启发式富化 → data/interim/enriched_*.json
    python3 -m packages.atomizer.emit      # 产出 data/atoms/{osaka,bangkok}.json + _registry.json
    python3 -m packages.atomizer.validate  # 质量门校验 + 报告
"""
