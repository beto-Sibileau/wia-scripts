"""Label-based bridge of MOZ adm2 P-codes: v1 (MSNA) -> v2 (current HDX).

Names are matched within the matching province (exact, then fuzzy within
the tolerance of label_bridge.yaml); manual pairs and v1 units with no v2
counterpart come from the same config (decided from the shape analysis of
build_bridge.py). Two steps: run, look at label_bridge_review.csv (units
left without a match, with their closest candidate), add the decisions to
the config, run again.

    python moz_pcode_bridge/label_bridge.py [--config other.yaml]

Outputs (data_out/geo_sight/moz_pcode_bridge/):
  label_bridge_v1_v2.csv   the mapping (one row per v1 x v2 pair)
  label_bridge_review.csv  units without a match (closest candidate, score)
  label_bridge_report.txt  coverage, comparison with REACH, name differences
"""

from __future__ import annotations

import argparse
from difflib import SequenceMatcher
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import yaml

from build_bridge import DEFAULTS, core, norm, read_reach

HERE = Path(__file__).resolve().parent
COLS = ["adm1_pcode_v1", "adm1_name_v1", "adm2_pcode_v1", "adm2_name_v1",
        "adm1_pcode_v2", "adm1_name_v2", "adm2_pcode_v2", "adm2_name_v2",
        "match_reason", "note"]


def read_names(zip_path) -> pd.DataFrame:
    g = gpd.read_file(f"zip://{zip_path}!moz_admin2.shp", ignore_geometry=True)
    return g[["adm1_pcode", "adm1_name", "adm2_pcode", "adm2_name"]]


def similarity(a: str, b: str) -> float:
    if norm(a) == norm(b):
        return 1.0
    return SequenceMatcher(None, core(a), core(b)).ratio()


def provinces(v1, v2, cfg) -> dict:
    """v1 adm1 P-code -> v2 adm1 P-code by name (aliases from config)."""
    p1 = v1.drop_duplicates("adm1_pcode").set_index("adm1_pcode").adm1_name
    p2 = v2.drop_duplicates("adm1_pcode").set_index("adm1_pcode").adm1_name
    alias = {norm(k): norm(v) for k, v in (cfg.get("province_aliases") or {}).items()}
    out = {}
    for c1, n1 in p1.items():
        target = alias.get(norm(n1), norm(n1))
        exact = [c2 for c2, n2 in p2.items() if norm(n2) == target]
        if exact:
            out[c1] = exact[0]
            continue
        best = max(p2.items(), key=lambda kv: similarity(n1, kv[1]))
        if similarity(n1, best[1]) >= cfg["fuzzy_threshold"]:
            out[c1] = best[0]
    return out


def match(v1, v2, cfg):
    thr = cfg["fuzzy_threshold"]
    manual = cfg.get("manual") or []
    not_in = {str(k): v for k, v in (cfg.get("not_in_v2") or {}).items()}
    notes = {str(k): v for k, v in (cfg.get("notes") or {}).items()}
    pairs = []                                    # (v1, v2, reason, note)
    for m in manual:
        for p2 in m["v2"]:
            pairs.append((m["v1"], p2, m["reason"], m.get("note", "")))
    for p1, note in not_in.items():
        pairs.append((p1, None, "not_in_v2", note))
    done1 = {p[0] for p in pairs}
    done2 = {p[1] for p in pairs if p[1]}

    prov = provinces(v1, v2, cfg)
    review = []
    for c1, grp in v1[~v1.adm2_pcode.isin(done1)].groupby("adm1_pcode"):
        cand = v2[(v2.adm1_pcode == prov.get(c1)) & ~v2.adm2_pcode.isin(done2)]
        scored = sorted(((similarity(o.adm2_name, n.adm2_name), o.adm2_pcode, n.adm2_pcode)
                         for o in grp.itertuples() for n in cand.itertuples()), reverse=True)
        used1, used2 = set(), set()
        for s, p1, p2 in scored:
            if s < thr or p1 in used1 or p2 in used2:
                continue
            used1.add(p1); used2.add(p2)
            exact = norm(v1.set_index("adm2_pcode").adm2_name[p1]) == \
                norm(v2.set_index("adm2_pcode").adm2_name[p2])
            pairs.append((p1, p2, "name_exact" if exact else "name_fuzzy", notes.get(p1, "")))
        done1 |= used1; done2 |= used2
        for o in grp.itertuples():
            if o.adm2_pcode in used1:
                continue
            best = next(((s, p2) for s, p1, p2 in scored if p1 == o.adm2_pcode), (np.nan, None))
            review.append({"side": "v1", "adm2_pcode": o.adm2_pcode, "adm2_name": o.adm2_name,
                           "closest_pcode": best[1], "score": round(best[0], 3)})
    for n in v2[~v2.adm2_pcode.isin(done2)].itertuples():
        same = v1[v1.adm1_pcode.map(prov) == n.adm1_pcode]
        best = max(((similarity(o.adm2_name, n.adm2_name), o.adm2_pcode) for o in same.itertuples()),
                   default=(np.nan, None))
        review.append({"side": "v2", "adm2_pcode": n.adm2_pcode, "adm2_name": n.adm2_name,
                       "closest_pcode": best[1], "score": round(best[0], 3)})
    return pairs, pd.DataFrame(review), prov


def table(pairs, v1, v2) -> pd.DataFrame:
    df = pd.DataFrame(pairs, columns=["adm2_pcode_v1", "adm2_pcode_v2", "match_reason", "note"])
    a = v1.add_suffix("_v1")
    b = v2.add_suffix("_v2")
    df = df.merge(a, on="adm2_pcode_v1", how="left").merge(b, on="adm2_pcode_v2", how="left")
    df["note"] = df["note"].fillna("").str.strip()
    return df[COLS].sort_values(["adm1_pcode_v1", "adm2_pcode_v1", "adm2_pcode_v2"]).reset_index(drop=True)


def setdiff(a, b) -> list:
    return sorted(np.setdiff1d(pd.Series(a).dropna().unique(), pd.Series(b).dropna().unique()).tolist())


def report(m, v1, v2, reach_raw, review) -> str:
    out = []
    p = out.append
    p(f"Mapping rows: {len(m)}; v1 units: {m.adm2_pcode_v1.nunique()} of {len(v1)}; "
      f"v2 units reached: {m.adm2_pcode_v2.nunique()} of {len(v2)}")
    p("match_reason: " + ", ".join(f"{k} {v}" for k, v in m.match_reason.value_counts().items()))
    n_src = m.dropna(subset=["adm2_pcode_v2"]).groupby("adm2_pcode_v2").size()
    p(f"v2 units with no v1 source: {setdiff(v2.adm2_pcode, m.adm2_pcode_v2)}")
    p(f"v2 units with several v1 sources: {n_src[n_src > 1].index.tolist()}")
    p(f"v1 units mapped to several v2: "
      f"{m.groupby('adm2_pcode_v1').adm2_pcode_v2.count().loc[lambda s: s > 1].index.tolist()}")
    p(f"units for review (no match within tolerance): {len(review)}")
    p("")
    p("== Comparison with the REACH bridge (v1 -> v2 pairs)")
    ours = set(zip(m.adm2_pcode_v1, m.adm2_pcode_v2.fillna("")))
    theirs = set(zip(reach_raw["2025_ADM2_PCODE"], reach_raw["ADM2_PCODE"].fillna("")))
    p(f"same pairs: {len(ours & theirs)}")
    p(f"pairs here not in REACH: {sorted(ours - theirs)}")
    p(f"pairs in REACH not here: {sorted(theirs - ours)}")
    p(f"v1 P-codes not in REACH: {setdiff(m.adm2_pcode_v1, reach_raw['2025_ADM2_PCODE'])}")
    p("")
    p("== Names: REACH bridge (ADM1_PT, ADM2_PT) vs shapefiles")
    for lv, col in ((1, "ADM1_PT"), (2, "ADM2_PT")):
        for ver, shp in (("v1", v1), ("v2", v2)):
            names = shp[f"adm{lv}_name"]
            p(f"adm{lv} in {ver} not in bridge: {setdiff(names, reach_raw[col])}")
            p(f"adm{lv} in bridge not in {ver}: {setdiff(reach_raw[col], names)}")
    p("")
    p("== Same, ignoring case and accents")
    for lv, col in ((1, "ADM1_PT"), (2, "ADM2_PT")):
        for ver, shp in (("v1", v1), ("v2", v2)):
            a = shp[f"adm{lv}_name"].map(norm); b = reach_raw[col].map(norm)
            p(f"adm{lv} in {ver} not in bridge: {setdiff(a, b)}")
            p(f"adm{lv} in bridge not in {ver}: {setdiff(b, a)}")
    p("")
    p("== Row by row: ADM2_PT vs v1 name of 2025_ADM2_PCODE (ignoring case/accents)")
    r = reach_raw.merge(v1, left_on="2025_ADM2_PCODE", right_on="adm2_pcode", how="left")
    bad = r[r.ADM2_PT.map(norm) != r.adm2_name.map(norm)]
    p(f"rows that differ: {len(bad)}")
    for _, x in bad.iterrows():
        p(f"  {x['2025_ADM2_PCODE']}: bridge '{x['ADM2_PT']}' vs v1 '{x['adm2_name']}'")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "label_bridge.yaml"))
    ap.add_argument("--old-zip", default=str(DEFAULTS["old_zip"]))
    ap.add_argument("--new-zip", default=str(DEFAULTS["new_zip"]))
    ap.add_argument("--reach", default=str(DEFAULTS["reach"]))
    ap.add_argument("--out", default=str(DEFAULTS["out"]))
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)

    v1, v2 = read_names(a.old_zip), read_names(a.new_zip)
    reach = read_reach(Path(a.reach))
    reach_raw = reach.rename(columns={"old_pcode": "2025_ADM2_PCODE", "new_pcode": "ADM2_PCODE",
                                      "reach_name": "ADM2_PT"})
    reach_raw["ADM1_PT"] = pd.read_csv(
        __import__("io").StringIO("\n".join(pd.read_excel(a.reach, header=None, dtype=str)[0])),
        dtype=str)["ADM1_PT"].values

    pairs, review, _ = match(v1, v2, cfg)
    m = table(pairs, v1, v2)
    m.to_csv(out / "label_bridge_v1_v2.csv", index=False, encoding="utf-8-sig")
    review.to_csv(out / "label_bridge_review.csv", index=False, encoding="utf-8-sig")
    txt = report(m, v1, v2, reach_raw, review)
    (out / "label_bridge_report.txt").write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
