#!/usr/bin/env python3
"""
Construit gnl.csv : bilans des banques centrales en milliards de USD, une ligne par jour.

Colonnes : time, fed, ecb, boj, pboc, boe, snb, boc, m2_us ... m2_br, us_debt
  fed  = Fed total assets (H.4.1, mercredi) - Reverse Repo (quotidien) - TGA (quotidien)
  ecb  = Eurosystem, bilan consolide hebdomadaire (ECB Data Portal, ILM)
  boj  = Bank of Japan Accounts, total actif, fin de mois (API BoJ)
  pboc = Bilan de l'autorite monetaire, fin de mois (PBoC, repli BIS)
  boe  = Bank of England, total actif, fin de mois (BIS, source Weekly Report BoE)
  snb  = BNS, total actif, fin de mois (data.snb.ch)
  boc  = Banque du Canada, total actif hebdomadaire (Valet)
  m2_xx = masse monetaire M2 : US (Fed H.6 hebdo), CN (PBoC), EU (BCE), JP (BoJ), GB (BoE, retail M4),
          CA (BoC), CH (BNS), TW (CBC), BR (BCB)   [Inde : pas de source ouverte automatisable]
  us_debt = dette publique federale totale (Tresor US, Debt to the Penny, quotidien)
Change : taux de reference BCE quotidiens (croises via l'EUR), TWD via FRED.

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
    rows = ecb_csv("EXR/D.USD+JPY+CNY+GBP+CHF+CAD+BRL.EUR.SP00.A?startPeriod=2013-10-01&format=csvdata")
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


import html.parser as _hp


class _Rows(_hp.HTMLParser):
    """Extrait les lignes de tableaux HTML (liste de listes de cellules texte)."""

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


def html_rows(raw):
    txt = None
    for enc in ("gbk", "gb18030", "utf-8"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            pass
    if txt is None:
        txt = raw.decode("utf-8", "ignore")
    p = _Rows()
    p.feed(txt)
    return p.rows


def pboc_row(rows, label_regex):
    """{fin de mois: valeur} pour la ligne dont la 1re cellule matche label_regex."""
    header = next(r for r in rows if sum(bool(re.fullmatch(r"\d{4}\.\d{1,2}", c)) for c in r) >= 1)
    line = next(r for r in rows if r and re.search(label_regex, r[0]))
    out = {}
    for h, v in zip(header, line):
        v = v.replace(",", "").strip()
        if re.fullmatch(r"\d{4}\.\d{1,2}", h) and re.fullmatch(r"-?\d+(\.\d+)?", v):
            y, mth = h.split(".")
            out[month_end(int(y), int(mth))] = float(v)
    return out


PBOC = "https://www.pbc.gov.cn"


def pboc_year_pages():
    """{annee: url de la page 'Money and Banking Statistics' (货币统计概览)}."""
    idx = get(f"{PBOC}/diaochatongjisi/116219/116319/index.html").decode("utf-8", "ignore")
    years = {}
    for href, y in re.findall(r'href=[\'"]([^\'"]+)[\'"][^>]*>\s*(20\d\d)年统计数据', idx):
        years.setdefault(int(y), urllib.parse.urljoin(PBOC + "/diaochatongjisi/116219/116319/index.html", href))
    pages = {}
    for y, url in sorted(years.items()):
        if y < 2013:
            continue
        try:
            yp = get(url, tries=2).decode("utf-8", "ignore")
            m = re.search(r'href=[\'"]([^\'"]+)[\'"][^>]*>\s*(?:<[^>]+>\s*)*货币统计概览', yp)
            if m:
                pages[y] = urllib.parse.urljoin(url, m.group(1))
        except Exception as e:  # noqa: BLE001
            log(f"  PBoC {y}: page annuelle indisponible ({e})")
    return pages


def pboc_series(section, label_regex, years=None):
    """Lit une serie mensuelle PBoC (100 millions CNY) sur plusieurs annees.
    section : texte de la ligne du tableau d'index (ex. '货币供应量')."""
    out = {}
    pages = pboc_year_pages()
    for y, url in sorted(pages.items()):
        if years and y not in years:
            continue
        try:
            page = get(url, tries=2).decode("utf-8", "ignore")
            pos = page.find(section)
            after = re.findall(r'href=[\'"]([^\'"]+\.htm)[\'"]', page[pos:] if pos >= 0 else page)[:6]
            others = [h for h in re.findall(r'href=[\'"]([^\'"]+\.htm)[\'"]', page) if h not in after]
            found = False
            for href in after + others[:30]:
                u = urllib.parse.urljoin(url, href).replace("http://", "https://")
                try:
                    vals = pboc_row(html_rows(get(u, tries=1)), label_regex)
                except Exception:  # noqa: BLE001  (lien mort ou autre tableau)
                    continue
                if vals:
                    out.update(vals)
                    found = True
                    break
            log(f"  PBoC {section} {y}: {'ok' if found else 'introuvable'}")
        except Exception as e:  # noqa: BLE001
            log(f"  PBoC {section} {y}: erreur ({e})")
    return out


def pboc_recent():
    """Annee en cours depuis le site de la PBoC : 100 millions CNY, fin de mois. {} si indisponible."""
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
            m = re.search(r'href=[\'"]([^\'"]+\.htm)[\'"]', idx[pos:pos + 3000])
            if not m:
                continue
            out.update(pboc_row(html_rows(get(base + m.group(1), tries=2)), r"总资产"))
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


# ---------------------------------------------------------------- M2 et dette

def mid_month(y, m):
    return dt.date(y, m, 15)


def us_debt():
    """Dette publique federale totale (Debt to the Penny), dollars, quotidien."""
    base = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/accounting/od/debt_to_penny"
    out, page = {}, 1
    while True:
        q = urllib.parse.urlencode({"fields": "record_date,tot_pub_debt_out_amt",
                                    "filter": "record_date:gte:2012-01-01",
                                    "page[size]": "10000", "page[number]": str(page)})
        j = json.loads(get(f"{base}?{q}").decode())
        for r in j["data"]:
            if r["tot_pub_debt_out_amt"] not in (None, "", "null"):
                out[dt.date.fromisoformat(r["record_date"])] = float(r["tot_pub_debt_out_amt"])
        if page >= j["meta"]["total-pages"]:
            break
        page += 1
    return out


def m2_eu():
    """Zone euro M2, stock fin de mois non CVS, millions EUR (BCE BSI)."""
    rows = ecb_csv("BSI/M.U2.N.V.M20.X.1.U2.2300.Z01.E?startPeriod=2012-01&format=csvdata")
    return {ym_end(r["TIME_PERIOD"]): float(r["OBS_VALUE"]) for r in rows if r["OBS_VALUE"]}


def m2_jp():
    """Japon M2, encours moyen du mois, 100 millions JPY (API BoJ)."""
    url = ("https://www.stat-search.boj.or.jp/api/v1/getDataCode?format=json&lang=en"
           "&db=MD02&code=MAM1NAM2M2MO&startDate=201201")
    v = json.loads(get(url).decode("utf-8-sig"))["RESULTSET"][0]["VALUES"]
    return {mid_month(int(str(d)[:4]), int(str(d)[4:6])): float(x)
            for d, x in zip(v["SURVEY_DATES"], v["VALUES"]) if x is not None}


def m2_gb():
    """Royaume-Uni M2 (retail M4), encours fin de mois non CVS, millions GBP (BoE LPMVQXV)."""
    url = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes"
           "&SeriesCodes=LPMVQXV&Datefrom=01/Jan/2012&Dateto=now&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")
    txt = get(url).decode("utf-8-sig")
    out = {}
    for row in list(csv.reader(io.StringIO(txt)))[1:]:
        if len(row) >= 2 and row[1].strip():
            out[dt.datetime.strptime(row[0].strip(), "%d %b %Y").date()] = float(row[1])
    if len(out) < 100:
        raise RuntimeError("BoE M2 : reponse inattendue")
    return out


def m2_ca():
    """Canada M2 (gross) non CVS, moyenne mensuelle, millions CAD (Valet V41552786)."""
    j = json.loads(get("https://www.bankofcanada.ca/valet/observations/V41552786/json?start_date=2012-01-01").decode())
    out = {}
    for o in j["observations"]:
        v = o.get("V41552786", {}).get("v")
        if v:
            d = dt.date.fromisoformat(o["d"])
            out[mid_month(d.year, d.month)] = float(v)
    return out


def m2_ch():
    """Suisse M2, fin de mois, millions CHF (data.snb.ch, cube snbmonagg)."""
    txt = get("https://data.snb.ch/api/cube/snbmonagg/data/csv/en?fromDate=2012-01").decode("utf-8-sig")
    out = {}
    for row in csv.reader(io.StringIO(txt), delimiter=";"):
        if len(row) == 4 and row[1] == "B" and row[2] == "GM2" and row[3]:
            out[ym_end(row[0])] = float(row[3])
    return out


def m2_tw():
    """Taiwan M2, moyenne journaliere du mois, millions TWD (CBC open data EF15M01)."""
    raw = get("https://www.cbc.gov.tw/public/data/OpenData/%E7%B6%93%E7%A0%94%E8%99%95/EF15M01.csv")
    txt = raw.decode("utf-8-sig", "ignore")
    rows = list(csv.reader(io.StringIO(txt)))
    col = next(i for i, h in enumerate(rows[0]) if "２" in h and "原始值" in h and "Ｍ" in h)
    out = {}
    for r in rows[1:]:
        m = re.fullmatch(r"(\d{4})M(\d{2})", r[0].strip())
        if m and r[col].strip() not in ("", "-"):
            out[mid_month(int(m.group(1)), int(m.group(2)))] = float(r[col])
    return out


def m2_br():
    """Bresil M2, fin de periode, milliers BRL (BCB SGS 27810)."""
    j = json.loads(get("https://api.bcb.gov.br/dados/serie/bcdata.sgs.27810/dados?formato=json&dataInicial=01/01/2012").decode())
    out = {}
    for o in j:
        d = dt.datetime.strptime(o["data"], "%d/%m/%Y").date()
        out[month_end(d.year, d.month)] = float(o["valor"])
    return out


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
    twd = fred("DEXTAUS")                 # TWD pour 1 USD
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

    log("M2 et dette...")
    m2 = dict(
        us=fred("WM2NS"),                 # milliards USD, hebdo
        cn=pboc_series("货币供应量", r"货币和准货币"),  # 100 millions CNY
        eu=m2_eu(),                       # millions EUR
        jp=m2_jp(),                       # 100 millions JPY
        gb=m2_gb(),                       # millions GBP
        ca=m2_ca(),                       # millions CAD
        ch=m2_ch(),                       # millions CHF
        tw=m2_tw(),                       # millions TWD
        br=m2_br(),                       # milliers BRL
    )
    debt = us_debt()                      # USD

    usd_per_eur = ffill(fx["USD"])
    per_eur = {c: ffill(fx[c]) for c in ("JPY", "CNY", "GBP", "CHF", "CAD", "BRL")}
    twd_per_usd = ffill(twd)
    series = dict(walcl=walcl, rrp=rrp, tga=tga, ecb=ecb, boj=boj, pboc=pboc, boe=boe, snb=snb, boc=boc,
                  debt=debt, **{"m2_" + k: v for k, v in m2.items()})
    f = {k: ffill(v) for k, v in series.items()}

    cols = ["fed", "ecb", "boj", "pboc", "boe", "snb", "boc",
            "m2_us", "m2_cn", "m2_eu", "m2_jp", "m2_gb", "m2_ca", "m2_ch", "m2_tw", "m2_br", "us_debt"]
    today = dt.datetime.now(dt.timezone.utc).date()
    lines = 0
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["time"] + cols)
        d = START - dt.timedelta(days=400)
        while d <= today:
            v = {k: g(d) for k, g in f.items()}
            eu = usd_per_eur(d)
            px = {c: g(d) for c, g in per_eur.items()}
            tw = twd_per_usd(d)
            if d >= START:
                missing = [k for k, x in list(v.items()) + list(px.items()) + [("EURUSD", eu), ("TWD", tw)] if x is None]
                if missing:
                    raise RuntimeError(f"donnee manquante au {d}: {missing}")
                usd = lambda cur: eu / px[cur]  # USD pour 1 unite de devise
                row = [
                    v["walcl"] / 1e3 - v["rrp"] - v["tga"] / 1e3,
                    v["ecb"] / 1e3 * eu,
                    v["boj"] / 10.0 * usd("JPY"),
                    v["pboc"] * usd("CNY"),
                    v["boe"] * usd("GBP"),
                    v["snb"] / 1e3 * usd("CHF"),
                    v["boc"] / 1e3 * usd("CAD"),
                    v["m2_us"],
                    v["m2_cn"] / 10.0 * usd("CNY"),
                    v["m2_eu"] / 1e3 * eu,
                    v["m2_jp"] / 10.0 * usd("JPY"),
                    v["m2_gb"] / 1e3 * usd("GBP"),
                    v["m2_ca"] / 1e3 * usd("CAD"),
                    v["m2_ch"] / 1e3 * usd("CHF"),
                    v["m2_tw"] / 1e3 / tw,
                    v["m2_br"] / 1e6 * usd("BRL"),
                    v["debt"] / 1e9,
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
            "snb": last(snb), "boc": last(boc), "fx_ecb": last(fx["USD"]), "fx_twd": last(twd),
            **{"m2_" + k: last(v) for k, v in m2.items()}, "us_debt": last(debt),
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
                  boe=(300, 2500), snb=(300, 2000), boc=(50, 800),
                  m2_us=(15000, 35000), m2_cn=(25000, 80000), m2_eu=(10000, 30000), m2_jp=(5000, 16000),
                  m2_gb=(2000, 6000), m2_ca=(1000, 4000), m2_ch=(500, 2500), m2_tw=(800, 3500),
                  m2_br=(600, 3500), us_debt=(25000, 60000))
    for (k, (lo, hi)), x in zip(bounds.items(), lastrow[1:]):
        if not lo <= float(x) <= hi:
            raise RuntimeError(f"valeur aberrante {k}={x} (attendu {lo}-{hi})")
    log(f"OK : {lines} lignes, derniere = {lastrow}")


if __name__ == "__main__":
    main(*sys.argv[1:])
