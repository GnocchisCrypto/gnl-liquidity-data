# indie:lang_version = 5
# Gnocchi liquidity - GNL 7 banques + Composite (RSI + M2 + NL) + M2 mondiale / Dette US
# Donnees : CSV quotidien (milliards USD) regenere automatiquement 2x/jour par GitHub Actions
# (github.com/GnocchisCrypto/gnl-liquidity-data) depuis les sources officielles.
# Unites : milliards USD. Poids/echelles TakeProfit = valeur TradingView x 1e9.
# Pour recharger les dernieres donnees : recharger la page ou re-ajouter l'indicateur.
from dataclasses import dataclass
from math import nan, isnan
from indie import indicator, MainContext, MutSeries, TimeFrame, request_series, param, plot, color
from indie.algorithms import Rsi, Ema, Sma
from indie.data import sources

CSV_URL = 'https://raw.githubusercontent.com/GnocchisCrypto/gnl-liquidity-data/main/gnl.csv'


@dataclass
class Gnl:
    fed: float
    ecb: float
    boj: float
    pboc: float
    boe: float
    snb: float
    boc: float
    m2_us: float
    m2_cn: float
    m2_eu: float
    m2_jp: float
    m2_gb: float
    m2_ca: float
    m2_ch: float
    m2_tw: float
    m2_br: float
    us_debt: float


def days_to_bars(tf: TimeFrame, days: int) -> int:
    bars = days
    if tf >= TimeFrame.from_str('1M'):
        bars = int(round(days / 30.0))
    elif tf >= TimeFrame.from_str('1W'):
        bars = int(round(days / 7.0))
    return bars


def momentum(cur: float, prev: float, pct: bool) -> float:
    res = nan
    if not isnan(cur) and not isnan(prev):
        if pct:
            if prev != 0:
                res = (cur - prev) / prev * 100.0
        else:
            res = cur - prev
    return res


@indicator('Gnocchi liquidity (TakeProfit)', overlay_main_pane=False)
# --- GNL 7 banques
@param.bool('show_gnl7', default=True, title='Afficher la courbe GNL 7 banques')
@param.float('scale', default=1.0, step=0.1, title="GNL : facteur d'echelle (x)")
@param.int('delta', default=52, min=1, title='GNL : periode Delta (barres)')
@param.int('shift_days', default=98, min=0, title='GNL : decalage avant (jours)')
@param.bool('pct', default=False, title="GNL : momentum en % (au lieu d'absolu)")
@param.bool('g_fed', default=False, title='GNL : Fed (net RRP/TGA)')
@param.bool('g_ecb', default=True, title='GNL : BCE')
@param.bool('g_boj', default=True, title='GNL : BoJ')
@param.bool('g_pboc', default=True, title='GNL : PBoC')
@param.bool('g_boe', default=True, title='GNL : BoE')
@param.bool('g_snb', default=False, title='GNL : BNS')
@param.bool('g_boc', default=False, title='GNL : BoC')
# --- Composite (RSI + M2 + NL)
@param.bool('show_composite', default=True, title='Afficher la courbe composite (moyenne)')
@param.int('rsi_len', default=14, min=1, title='Composite : longueur RSI')
@param.int('c_shift_days', default=56, min=0, title='Composite : decalage avant (jours)')
@param.int('c_delta', default=52, min=1, title='Composite : periode Delta M2/NL (barres)')
@param.str('var_type', default='Absolu ($)', options=['Absolu ($)', 'Pourcentage (%)'], title='Composite : type de variation')
@param.bool('inc_rsi', default=True, title='Composite : inclure RSI')
@param.float('w_rsi', default=1.0, min=0.0, step=0.1, title='Composite : poids RSI (x)')
@param.bool('inc_m2', default=True, title='Composite : inclure M2 mom.')
@param.float('w_m2', default=1.0, step=0.0001, title='Composite : poids M2 (x)')
@param.bool('inc_nl', default=True, title='Composite : inclure NL mom.')
@param.float('w_nl', default=1.0, step=0.0001, title='Composite : poids NL (x)')
# --- M2 mondiale / Dette US
@param.bool('show_ratio', default=True, title='Afficher M2/Dette momentum')
@param.float('ratio_scale', default=1.0, step=0.1, title="M2/Dette : facteur d'echelle (x)")
@param.int('ratio_len', default=91, min=1, title='M2/Dette : momentum length (barres)')
@param.int('ratio_shift', default=91, min=0, title='M2/Dette : decalage avant (jours)')
@param.str('ratio_type', default='ROC', options=['ROC', 'Difference', 'EMA Slope'], title='M2/Dette : type de momentum')
@param.int('ratio_smooth', default=10, min=1, title='M2/Dette : lissage (SMA)')
@plot.line(title='GNL 7 banques', color=color.rgba(79, 214, 200), line_width=2)
@plot.line(title='Composite', color=color.ORANGE, line_width=2)
@plot.line(title='M2/Dette', color=color.rgba(0, 188, 212), line_width=2)
class Main(MainContext):
    def __init__(self, show_gnl7, scale, delta, shift_days, pct, g_fed, g_ecb, g_boj, g_pboc, g_boe, g_snb, g_boc,
                 show_composite, rsi_len, c_shift_days, c_delta, var_type, inc_rsi, w_rsi, inc_m2, w_m2, inc_nl, w_nl,
                 show_ratio, ratio_scale, ratio_len, ratio_shift, ratio_type, ratio_smooth):
        self._show_gnl7 = show_gnl7
        self._scale = scale
        self._delta = delta
        self._pct = pct
        self._flags = [g_fed, g_ecb, g_boj, g_pboc, g_boe, g_snb, g_boc]
        self._show_c = show_composite
        self._rsi_len = rsi_len
        self._c_delta = c_delta
        self._c_pct = var_type == 'Pourcentage (%)'
        self._inc_rsi = inc_rsi
        self._w_rsi = w_rsi
        self._inc_m2 = inc_m2
        self._w_m2 = w_m2
        self._inc_nl = inc_nl
        self._w_nl = w_nl
        self._show_r = show_ratio
        self._r_scale = ratio_scale
        self._r_len = ratio_len
        self._r_type = ratio_type
        self._r_smooth = ratio_smooth
        tf = self.time_frame
        self._bars = days_to_bars(tf, shift_days)
        self._c_bars = days_to_bars(tf, c_shift_days)
        self._r_bars = days_to_bars(tf, ratio_shift)
        self._data = request_series[Gnl](source=sources.Csv(CSV_URL))

    def calc(self):
        r = self._data.get(0, Gnl(nan, nan, nan, nan, nan, nan, nan, nan, nan, nan, nan, nan, nan, nan, nan, nan, nan))

        # ---------------- GNL 7 banques
        vals = [r.fed, r.ecb, r.boj, r.pboc, r.boe, r.snb, r.boc]
        total = 0.0
        for i in range(7):
            if self._flags[i]:
                total += vals[i]
        tot = MutSeries[float].new(init=nan)
        tot[0] = total
        gnl_mom = momentum(total, tot.get(self._delta, nan), self._pct)
        gnl_out = gnl_mom * self._scale if self._show_gnl7 else nan

        # ---------------- Composite : RSI + M2 mondiale + Net Liquidity
        glm2 = r.m2_us + r.m2_cn + r.m2_eu + r.m2_jp + r.m2_gb + r.m2_ca + r.m2_ch + r.m2_tw + r.m2_br
        nl = r.fed + r.ecb + r.boj + r.pboc + r.boe
        m2s = MutSeries[float].new(init=nan)
        m2s[0] = glm2
        nls = MutSeries[float].new(init=nan)
        nls[0] = nl
        m2_mom = momentum(glm2, m2s.get(self._c_delta, nan), self._c_pct)
        nl_mom = momentum(nl, nls.get(self._c_delta, nan), self._c_pct)

        rsi = Rsi.new(self.close, self._rsi_len)
        rsi_scaled = rsi.get(self._c_bars, nan) * self._w_rsi
        m2_scaled = m2_mom * self._w_m2
        nl_scaled = nl_mom * self._w_nl
        rsi_ok = self._inc_rsi and not isnan(rsi_scaled)
        m2_ok = self._inc_m2 and not isnan(m2_scaled)
        nl_ok = self._inc_nl and not isnan(nl_scaled)
        n = 0
        s = 0.0
        if rsi_ok:
            n += 1
            s += rsi_scaled
        if m2_ok:
            n += 1
            s += m2_scaled
        if nl_ok:
            n += 1
            s += nl_scaled
        composite = s / n if n > 0 else nan
        comp_out = composite if self._show_c else nan

        # ---------------- M2 mondiale (US+CN+EU+JP) / Dette US
        ratio = nan
        if r.us_debt != 0 and not isnan(r.us_debt):
            ratio = (r.m2_us + r.m2_cn + r.m2_eu + r.m2_jp) / r.us_debt
        rs = MutSeries[float].new(init=nan)
        rs[0] = ratio
        ema = Ema.new(rs, self._r_len)
        prev = rs.get(self._r_len, nan)
        raw = nan
        if self._r_type == 'ROC':
            if not isnan(prev) and prev != 0 and not isnan(ratio):
                raw = 100.0 * (ratio - prev) / prev
        elif self._r_type == 'Difference':
            if not isnan(prev) and not isnan(ratio):
                raw = ratio - prev
        else:
            raw = ema[0] - ema.get(self._r_len, nan)
        raws = MutSeries[float].new(init=nan)
        raws[0] = raw
        r_mom = Sma.new(raws, self._r_smooth)[0]
        ratio_out = r_mom * self._r_scale if self._show_r else nan

        return (plot.Line(gnl_out, offset=self._bars),
                plot.Line(comp_out, offset=self._c_bars),
                plot.Line(ratio_out, offset=self._r_bars))
