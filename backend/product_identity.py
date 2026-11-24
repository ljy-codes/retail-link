"""Pure, shared product/location identity rules. Names are display data only."""
from __future__ import annotations

import hashlib
import json

CATALOG_FIELDS = ("sourceProduct", "sourceShop", "product", "name", "specification", "shop",
                  "sourceSystem", "sourceSku", "sourceSpecification", "sourceWarehouseId", "warehouseId")
CATALOG_COLUMNS = set(CATALOG_FIELDS) | {"catalogId", "source", "revision", "updatedAt"}
DAILY_IDENTITY_FIELDS = ("sourceSystem", "sourceSku", "specification", "warehouseId")


def text(value):
    return str(value).strip() if value is not None else ""


def digest(values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def catalog_identity(record):
    location = ("id", text(record.get("sourceWarehouseId"))) if text(record.get("sourceWarehouseId")) else ("name", text(record.get("sourceShop")))
    sku = text(record.get("sourceSku"))
    return digest([text(record.get("sourceSystem")), text(record.get("sourceProduct")), sku, "" if sku else text(record.get("sourceSpecification"))] + [location])


def normalize_catalog(record):
    if not isinstance(record, dict):
        raise ValueError("商品对照应为对象。")
    normalized = {key: text(record.get(key)) for key in CATALOG_FIELDS}
    if not normalized["sourceProduct"] or not normalized["product"]:
        raise ValueError("来源商品ID与标准商品标识均不能为空。")
    if any(len(value) > (500 if key == "name" else 200) for key, value in normalized.items()):
        raise ValueError("商品对照字段过长。")
    # Legacy callers had just one specification field. An explicit empty source
    # specification remains empty when editing a migrated alias.
    if "sourceSpecification" not in record:
        normalized["sourceSpecification"] = normalized["specification"]
    normalized["name"] = normalized["name"] or normalized["product"]
    normalized["source"] = record.get("source") or "人工确认"
    json.dumps(normalized["source"], ensure_ascii=False, allow_nan=False)
    normalized["catalogId"] = catalog_identity(normalized)
    return normalized


def resolve_identity(catalog, product, warehouse="", specification="", source_sku="", warehouse_id="", source_system=""):
    product, warehouse, specification, source_sku, warehouse_id, source_system = map(text, (product, warehouse, specification, source_sku, warehouse_id, source_system))
    result = {"product": product, "name": product, "specification": specification,
              "warehouse": warehouse, "warehouseId": warehouse_id, "sourceProduct": product,
              "sourceSku": source_sku, "sourceSystem": source_system, "sourceWarehouseId": warehouse_id,
              "catalogId": "", "catalogRevision": None, "identityStatus": "unmapped"}
    candidates = []
    for raw in catalog or []:
        if not isinstance(raw, dict) or text(raw.get("sourceProduct")) != product:
            continue
        item = normalize_catalog(raw)
        if item["sourceSystem"] != source_system:
            continue
        if warehouse_id:
            if item["sourceWarehouseId"] != warehouse_id:
                continue
        elif warehouse and item["sourceShop"] != warehouse:
            continue
        if source_sku and item["sourceSku"] != source_sku:
            continue
        if specification and not source_sku and item["sourceSpecification"] != specification:
            continue
        candidates.append((item, raw))
    if not candidates and not any(isinstance(raw, dict) and text(raw.get("sourceProduct")) == product for raw in catalog or []):
        standards = [normalize_catalog(raw) for raw in catalog or [] if isinstance(raw, dict) and text(raw.get("product")) == product]
        standards = [item for item in standards if not specification or item["specification"] == specification]
        if standards and len({item["specification"] for item in standards}) == 1:
            result.update(name=standards[0]["name"], specification=standards[0]["specification"],
                          catalogId=standards[0]["catalogId"], catalogRevision=max(raw.get("revision", 1) for raw in catalog or [] if isinstance(raw, dict) and text(raw.get("product")) == product),
                          catalogIds=[item["catalogId"] for item in standards], identityStatus="mapped")
            return result
    # No ID in a legacy source can match an ID-backed alias by its name only
    # when that name points to one location. Never pick among same-name IDs.
    product_only = not warehouse_id and not warehouse and bool(candidates) and len({(item["product"], item["specification"]) for item, _ in candidates}) == 1
    if len(candidates) == 1 or product_only:
        item, raw = candidates[0]
        result.update(product=item["product"], name=item["name"], specification=item["specification"],
                      warehouse=item["shop"], warehouseId=item["warehouseId"] or item["sourceWarehouseId"],
                      catalogId=item["catalogId"], catalogRevision=raw.get("revision", 1), identityStatus="mapped")
        if product_only:
            result.update(warehouse="", warehouseId="", catalogIds=[item["catalogId"] for item, _ in candidates])
    elif candidates:
        result["identityStatus"] = "ambiguous"
    return result


def daily_identity(record):
    source = record.get("source")
    mapped = isinstance(source, dict) and bool(source.get("catalog"))
    location = ("id", text(record.get("warehouseId"))) if text(record.get("warehouseId")) else ("name", text(record.get("shop")))
    return digest([text(record.get(key)) for key in ("date", "product", "specification")]
                  + (["", ""] if mapped else [text(record.get("sourceSystem")), text(record.get("sourceSku"))]) + [location])
