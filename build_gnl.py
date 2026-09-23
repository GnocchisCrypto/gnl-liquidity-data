#!/usr/bin/env python3
"""
Construit gnl.csv : bilans des banques centrales en milliards de USD, une ligne par jour.

Colonnes : time, fed, ecb, boj, pboc, boe, snb, boc
  fed  = Fed total assets (H.4.1, mercredi) - Reverse Repo (quotidien) - TGA (quotidien)
  ecb  = Eurosystem, bilan consolide hebdomadaire (ECB Data Portal, ILM)
  boj  = Bank of Japan Accounts, total actif, fin de mois (API BoJ)
  pboc = Bilan de l'autorite monetaire, fin de mois (PBoC, repli BIS)
  boe  = Bank of England, total actif, fin de mois (BIS, source Weekly Report BoE)
  snb  = BNS, total actif, fin de mois (data.snb.ch)
  boc  = Banque du Canada, total actif hebdomadaire (Valet)
Change : taux de reference BCE quotidiens (croises via l'EUR).

Chaque valeur est datee a sa date d'observation reelle, puis prolongee
(forward-fill) jusqu'a la publication suivante. Aucune dependance externe.
"""
import csv
import datetime as dt
import html
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request

START = dt.date(2014, 1, 1)
UA = {"User-Agent": "Mozilla/5.0 (gnl-liquidity-data; +https://github.com)"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, tries=4, timeout=90):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  retry {i + 1}/{tries} {url[:90]}... ({e})")
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"echec telechargement {url}: {last}")


def month_end(y, m):
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def ym_end(s):  # '2026-07' ou '202607' -> date fin de mois
    s = s.replace("-", "")
    return month_end(int(s[:4]), int(s[4:6]))


# ---------------------------------------------------------------- sources

def fred(series):
    txt = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}").decode()
    out = {}
    for row in list(csv.reader(io.StringIO(txt)))[1:]:
        if len(row) >= 2 and row[1] not in ("", "."):
            out[dt.date.fromisoformat(row[0])] = float(row[1])
    return out


def ecb_csv(path):
    url = "https://data-api.ecb.europa.eu/service/data/" + path
    txt = get(url).decode()
    return list(csv.DictReader(io.StringIO(txt)))


def ecb_fx():
    """{devise: {date: unites de devise pour 1 EUR}}"""
    rows = ecb_csv("EXR/D.USD+JPY+CNY+GBP+CHF+CAD.EUR.SP00.A?startPeriod=2013-10-01&format=csvdata")
    fx = {}
    for r in rows:
        if r["OBS_VALUE"]:
            fx.setdefault(r["CURRENCY"], {})[dt.date.fromisoformat(r["TIME_PERIOD"])] = float(r["OBS_VALUE"])
    return fx


def ecb_assets():
    """Eurosystem total actif, millions EUR, date = vendredi de la semaine de reference."""
    rows = ecb_csv("ILM/W.U2.C.T000000.Z5.Z01?startPeriod=2013-10-01&format=csvdata")
    out = {}
    for r in rows:
        if not r["OBS_VALUE"]:
            continue
        m = re.match(r"(\d{4})-W(\d{2})", r["TIME_PERIOD"])
        d = dt.date.fromisocalendar(int(m.group(1)), int(m.group(2)), 5)
        out[d] = float(r["OBS_VALUE"])
    return out


def boj_assets():
    """BoJ total actif, 100 millions JPY, fin de mois."""
    url = ("https://www.stat-search.boj.or.jp/api/v1/getDataCode?format=json&lang=en"
           "&db=BS01&code=MABJMTA&startDate=201310")
    j = json.loads(get(url).decode("utf-8-sig"))
    v = j["RESULTSET"][0]["VALUES"]
    return {ym_end(str(d)): float(x) for d, x in zip(v["SURVEY_DATES"], v["VALUES"]) if x is not None}


def bis_cbta(area):
    """BIS WS_CBTA, milliards de monnaie locale, fin de mois (serie non ajustee)."""
    url = f"https://stats.bis.org/api/v1/data/WS_CBTA/M.{area}..?startPeriod=2012-01&format=csv"
    rows = list(csv.DictReader(io.StringIO(get(url).decode("utf-8-sig"))))
    series = {}
    for r in rows:
        if r.get("UNIT_MEASURE") != "XDC" or not r.get("OBS_VALUE"):
            continue
        series.setdefault(r.get("TRANSFORMATION", ""), {})[ym_end(r["TIME_PERIOD"])] = float(r["OBS_VALUE"])
    key = "N" if "N" in series else sorted(series)[0]
    return series[key]


def pboc_recent():
    """Annee en cours depuis le site de la PBoC : 100 millions CNY, fin de mois. {} si indisponible."""
    import html.parser as hp

    class T(hp.HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows, self.row, self.cell, self.inc = [], None, "", False

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self.row = []
            elif tag in ("td", "th"):
                self.inc, self.cell = True, ""

        def handle_endtag(self, tag):
            if tag in ("td", "th") and self.row is not None:
                self.row.append(re.sub(r"\s+", " ", self.cell).strip())
                self.inc = False
            elif tag == "tr" and self.row is not None:
                self.rows.append(self.row)
                self.row = None

        def handle_data(self, d):
            if self.inc:
                self.cell += d

    out = {}
    base = "https://www.pbc.gov.cn"
    today = dt.date.today()
    for year in sorted({today.year, (today - dt.timedelta(days=60)).year}):
        try:
            idx = get(f"{base}/diaochatongjisi/116219/116319/{year}ntjsj/hbtjgl/index.html", tries=2).decode("utf-8", "ignore")
            # ligne "货币当局资产负债表" -> lien .htm
            pos = idx.find("货币当局资产负债表")
            if pos < 0:
                log(f"  PBoC {year}: tableau introuvable")
                continue
            m = re.search(r'href="([^"]+\.htm)"', idx[pos:pos + 3000])
            if not m:
                continue
            raw = get(base + m.group(1), tries=2)
            txt = None
            for enc in ("gbk", "gb18030", "utf-8"):
                try:
                    txt = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    pass
            p = T()
            p.feed(txt)
            header = next(r for r in p.rows if any(re.fullmatch(r"\d{4}\.\d{2}", c) for c in r))
            total = next(r for r in p.rows if r and "总资产" in r[0])
            for h, v in zip(header, total):
                if re.fullmatch(r"\d{4}\.\d{2}", h) and v.replace(",", ""):
                    y, mth = h.split(".")
                    out[month_end(int(y), int(mth))] = float(v.replace(",", ""))
            log(f"  PBoC {year}: {len(out)} mois lus")
        except Exception as e:  # noqa: BLE001
            log(f"  PBoC {year}: indisponible ({e}) -> repli BIS")
    return out


def snb_assets():
    """BNS total actif (T0), millions CHF, fin de mois."""
    txt = get("https://data.snb.ch/api/cube/snbbipo/data/csv/en?fromDate=2012-01").decode("utf-8-sig")
    out = {}
    for row in csv.reader(io.StringIO(txt), delimiter=";"):
        if len(row) == 3 and row[1] == "T0" and row[2]:
            out[ym_end(row[0])] = float(row[2])
    return out


def boc_assets():
    """Banque du Canada total actif, millions CAD, hebdomadaire (mercredi)."""
    j = json.loads(get("https://www.bankofcanada.ca/valet/observations/V36610/json?start_date=2012-01-01").decode())
    return {dt.date.fromisoformat(o["d"]): float(o["V36610"]["v"]) for o in j["observations"] if o.get("V36610", {}).get("v")}


def tga_daily():
    """Solde du TGA, millions USD, quotidien (Daily Treasury Statement). Repli FRED WTREGEN."""
    try:
        base = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance"
        out, page = {}, 1
        while True:
            q = urllib.parse.urlencode({
                "fields": "record_date,account_type,open_today_bal,close_today_bal",
                "filter": "record_date:gte:2013-10-01",
                "page[size]": "10000", "page[number]": str(page)})
            j = json.loads(get(f"{base}?{q}").decode())
            for r in j["data"]:
                at = r["account_type"]
                if at == "Treasury General Account (TGA) Closing Balance":
                    v = r["open_today_bal"]
                elif at == "Federal Reserve Account":
                    v = r["close_today_bal"]
                else:
                    continue
                if v not in (None, "", "null"):
                    out[dt.date.fromisoformat(r["record_date"])] = float(v)
            if page >= j["meta"]["total-pages"]:
                break
            page += 1
        if len(out) < 1000:
            raise RuntimeError(f"trop peu de points TGA ({len(out)})")
        return out, "Treasury DTS (quotidien)"
    except Exception as e:  # noqa: BLE001
        log(f"  TGA quotidien indisponible ({e}) -> FRED WTREGEN")
        return fred("WTREGEN"), "FRED WTREGEN (hebdo)"


# ---------------------------------------------------------------- assemblage

def ffill(series):
    items = sorted(series.items())
    i, cur = 0, None

    def at(d):
        nonlocal i, cur
        while i < len(items) and items[i][0] <= d:
            cur = items[i][1]
            i += 1
        return cur
    return at


def main(out_path="gnl.csv", status_path="status.json"):
    log("Telechargement...")
    fx = ecb_fx()
    walcl = fred("WALCL")                 # millions USD
    rrp = fred("RRPONTSYD")               # milliards USD
    tga, tga_src = tga_daily()            # millions USD
    ecb = ecb_assets()                    # millions EUR
    boj = boj_assets()                    # 100 millions JPY
    pboc = bis_cbta("CN")                 # milliards CNY
    pboc_src = "BIS"
    recent = pboc_recent()                # 100 millions CNY
    if recent:
        pboc.update({d: v / 10.0 for d, v in recent.items()})
        pboc_src = "PBoC (annee en cours) + BIS (historique)"
    boe = bis_cbta("GB")                  # milliards GBP
    snb = snb_assets()                    # millions CHF
    boc = boc_assets()                    # millions CAD

    usd_per_eur = ffill(fx["USD"])
    per_eur = {c: ffill(fx[c]) for c in ("JPY", "CNY", "GBP", "CHF", "CAD")}
    f = {k: ffill(v) for k, v in dict(walcl=walcl, rrp=rrp, tga=tga, ecb=ecb, boj=boj,
                                        pboc=pboc, boe=boe, snb=snb, boc=boc).items()}

    today = dt.datetime.now(dt.timezone.utc).date()
    lines = 0
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["time", "fed", "ecb", "boj", "pboc", "boe", "snb", "boc"])
        d = START - dt.timedelta(days=120)
        while d <= today:
            v = {k: g(d) for k, g in f.items()}
            eu = usd_per_eur(d)
            px = {c: g(d) for c, g in per_eur.items()}
            if d >= START:
                if any(x is None for x in list(v.values()) + list(px.values()) + [eu]):
                    raise RuntimeError(f"donnee manquante au {d}: {v} {px}")
                usd = lambda cur: eu / px[cur]  # USD pour 1 unite de devise
                row = [
                    v["walcl"] / 1e3 - v["rrp"] - v["tga"] / 1e3,
                    v["ecb"] / 1e3 * eu,
                    v["boj"] / 10.0 * usd("JPY"),
                    v["pboc"] * usd("CNY"),
                    v["boe"] * usd("GBP"),
                    v["snb"] / 1e3 * usd("CHF"),
                    v["boc"] / 1e3 * usd("CAD"),
                ]
                w.writerow([d.isoformat() + "T00:00:00Z"] + [f"{x:.2f}" for x in row])
                lines += 1
            d += dt.timedelta(days=1)

    last = lambda s: max(s).isoformat()
    status = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "rows": lines,
        "unit": "milliards USD",
        "derniere_observation": {
            "fed_total_assets": last(walcl), "fed_rrp": last(rrp), "fed_tga": last(tga),
            "ecb": last(ecb), "boj": last(boj), "pboc": last(pboc), "boe": last(boe),
            "snb": last(snb), "boc": last(boc), "fx_ecb": last(fx["USD"]),
        },
        "sources": {"tga": tga_src, "pboc": pboc_src},
    }
    with open(status_path, "w") as fh:
        json.dump(status, fh, indent=2, ensure_ascii=False)
    log(json.dumps(status, indent=2, ensure_ascii=False))

    # garde-fous : ordres de grandeur (milliards USD) sur la derniere ligne
    with open(out_path) as fh:
        lastrow = list(csv.reader(fh))[-1]
    bounds = dict(fed=(2000, 12000), ecb=(2000, 12000), boj=(1500, 9000), pboc=(3000, 12000),
                  boe=(300, 2500), snb=(300, 2000), boc=(50, 800))
    for (k, (lo, hi)), x in zip(bounds.items(), lastrow[1:]):
        if not lo <= float(x) <= hi:
            raise RuntimeError(f"valeur aberrante {k}={x} (attendu {lo}-{hi})")
    log(f"OK : {lines} lignes, derniere = {lastrow}")


if __name__ == "__main__":
    main(*sys.argv[1:])
