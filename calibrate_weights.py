"""
calibrate_weights.py - Factor weight optimizer for Polymarket bot
Uses resolved picks from backtest.db to find empirically optimal weights.

Approach:
  1. Logistic regression (L2 regularized) -- coefficients = relative predictive power
  2. Grid search (scipy.optimize) -- directly maximizes ROI with weight constraints
  3. Bootstrap CIs -- tells you which factors are reliably non-zero
  4. Brier score -- measures calibration quality before/after

Usage:
  python calibrate_weights.py             # report only
  python calibrate_weights.py --apply     # write optimal weights to .env
"""

import sqlite3
import argparse
import warnings
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")

# --- path resolution (handles Windows/Linux mount mismatch) -------------------
def _find_file(name):
    for candidate in [Path(name), Path.cwd() / name]:
        if candidate.exists():
            return candidate
    try:
        here = Path(__file__).resolve().parent
        candidate = here / name
        if candidate.exists():
            return candidate
    except NameError:
        pass
    raise FileNotFoundError(f"{name} not found (searched cwd and script dir)")

DB_PATH = _find_file("backtest.db")
ENV_PATH = Path(".env")  # write to cwd

FACTORS = ["score_leaderboard", "score_fair_value", "score_line_movement", "score_news", "score_urgency"]
FACTOR_LABELS = ["leaderboard", "fair_value", "line_movement", "news", "urgency"]
CURRENT_WEIGHTS = [30, 30, 20, 10, 10]


# --- data loading -------------------------------------------------------------
def load_data():
    import shutil, tempfile
    # OneDrive/FUSE mounts don't support SQLite locking -- copy to /tmp first
    tmp = Path(tempfile.mktemp(suffix=".db"))
    shutil.copy2(str(DB_PATH), str(tmp))
    conn = sqlite3.connect(str(tmp))
    cur = conn.cursor()
    cols = ", ".join(FACTORS)
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(picks)")}
    cat_col = "category" if "category" in existing_cols else "'' as category"
    cur.execute(f"""
        SELECT {cols}, combined_score, entry_price, pnl, won, {cat_col}
        FROM picks
        WHERE won IS NOT NULL
    """)
    rows = cur.fetchall()
    conn.close()

    if not rows:
        raise ValueError("No resolved picks in backtest.db")

    categories = [r[9] for r in rows]
    arr = np.array([r[:9] for r in rows], dtype=float)
    X = arr[:, :5]
    combined = arr[:, 5]
    entry_prices = arr[:, 6]
    pnl = arr[:, 7]
    y = arr[:, 8].astype(int)
    return X, y, combined, entry_prices, pnl, categories


def normalize_weights(w):
    w = np.clip(np.array(w, dtype=float), 0, None)
    total = w.sum()
    return w / total * 100 if total > 0 else np.ones(len(w)) * (100 / len(w))


def brier_score(y_true, y_prob):
    return float(np.mean((y_prob - y_true) ** 2))


def compute_baseline_brier(X, y, weights):
    w = np.array(weights, dtype=float)
    w /= w.sum()
    max_scores = np.array([30.0, 30.0, 20.0, 10.0, 10.0])
    scores = X @ w
    probs = np.clip(scores / (max_scores @ w), 0.01, 0.99)
    return brier_score(y, probs)


# --- logistic regression ------------------------------------------------------
def run_logistic_regression(X, y):
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import StratifiedKFold, cross_val_score
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline
    except ImportError:
        print("  [!] scikit-learn not installed: pip install scikit-learn --break-system-packages")
        return None, None, None

    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("lr", LogisticRegression(penalty="l2", C=1.0, max_iter=1000, solver="lbfgs"))
    ])

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    auc_scores = cross_val_score(pipe, X, y, cv=cv, scoring="roc_auc")

    pipe.fit(X, y)
    lr = pipe.named_steps["lr"]
    scaler = pipe.named_steps["scaler"]
    coef = lr.coef_[0]
    std = scaler.scale_
    importance = coef * std

    probs = pipe.predict_proba(X)[:, 1]
    bs = brier_score(y, probs)

    return importance, auc_scores, bs


def bootstrap_importance(X, y, n_bootstrap=1000):
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline
    except ImportError:
        return None

    n = len(y)
    importances = []
    rng = np.random.default_rng(42)

    for _ in range(n_bootstrap):
        idx = rng.integers(0, n, size=n)
        Xb, yb = X[idx], y[idx]
        if len(np.unique(yb)) < 2:
            continue
        try:
            pipe = Pipeline([
                ("scaler", StandardScaler()),
                ("lr", LogisticRegression(penalty="l2", C=1.0, max_iter=500, solver="lbfgs"))
            ])
            pipe.fit(Xb, yb)
            coef = pipe.named_steps["lr"].coef_[0]
            std = pipe.named_steps["scaler"].scale_
            importances.append(coef * std)
        except Exception:
            continue

    return np.array(importances) if importances else None


# --- ROI optimization ---------------------------------------------------------
def optimize_weights_roi(X, y, pnl):
    try:
        from scipy.optimize import differential_evolution
    except ImportError:
        print("  [!] scipy not installed: pip install scipy --break-system-packages")
        return None

    n = len(y)
    top_k = max(20, n // 4)

    def neg_roi(w):
        w = np.clip(w, 0, None)
        if w.sum() < 1e-9:
            return 0.0
        w = w / w.sum()
        scores = X @ w
        top_idx = np.argsort(scores)[-top_k:]
        return -pnl[top_idx].mean() if len(top_idx) else 0.0

    bounds = [(0, 1)] * 5
    result = differential_evolution(
        neg_roi, bounds, seed=42, maxiter=500, popsize=15, tol=1e-6, polish=True
    )
    return result.x


# --- reporting ----------------------------------------------------------------
def sep(title=""):
    line = "-" * 60
    if title:
        print(f"\n{line}\n  {title}\n{line}")
    else:
        print(line)


def main(apply=False):
    print("\n" + "=" * 60)
    print("  POLYMARKET FACTOR WEIGHT CALIBRATION")
    print("=" * 60)

    X, y, combined, entry_prices, pnl, categories = load_data()
    n = len(y)
    wins = int(y.sum())
    win_rate = wins / n

    sep("DATASET SUMMARY")
    print(f"  Resolved picks : {n}")
    print(f"  Wins / Losses  : {wins} / {n - wins}  ({win_rate:.1%} win rate)")
    print(f"  Total PnL      : ${pnl.sum():.2f}")
    print(f"  Avg PnL/pick   : ${pnl.mean():.4f}")

    # Category breakdown
    unique_cats = sorted(set(c for c in categories if c))
    if unique_cats:
        sep("WIN RATE BY MARKET CATEGORY")
        print(f"  {'Category':<18} {'Picks':>6} {'Wins':>6} {'Win%':>7} {'Avg PnL':>9}")
        print(f"  {'-'*18} {'-'*6} {'-'*6} {'-'*7} {'-'*9}")
        cat_arr = np.array(categories)
        for cat in unique_cats:
            mask = cat_arr == cat
            cat_y = y[mask]
            cat_pnl = pnl[mask]
            if len(cat_y) == 0:
                continue
            cat_wins = cat_y.sum()
            print(f"  {cat:<18} {len(cat_y):>6} {cat_wins:>6} {cat_wins/len(cat_y):>7.1%} {cat_pnl.mean():>9.4f}")
        no_cat = (cat_arr == "").sum()
        if no_cat:
            print(f"  {'(untagged)':<18} {no_cat:>6}  (logged before category tracking)")
    else:
        print("  No category data yet — will populate on next bot run.")

    sep("FACTOR CORRELATION WITH WINS (raw)")
    print(f"  {'Factor':<18} {'Win avg':>9} {'Loss avg':>9} {'Diff':>8}  p-value")
    print(f"  {'-'*18} {'-'*9} {'-'*9} {'-'*8}  -------")
    from scipy import stats as spstats
    for i, label in enumerate(FACTOR_LABELS):
        win_vals = X[y == 1, i]
        loss_vals = X[y == 0, i]
        w_mean = win_vals.mean()
        l_mean = loss_vals.mean()
        diff = w_mean - l_mean
        if len(win_vals) > 1 and len(loss_vals) > 1:
            _, pval = spstats.ttest_ind(win_vals, loss_vals)
            sig = f"p={pval:.3f} {'**' if pval < 0.05 else ('*' if pval < 0.10 else '')}"
        else:
            sig = "?"
        print(f"  {label:<18} {w_mean:>9.2f} {l_mean:>9.2f} {diff:>+8.2f}  {sig}")

    sep("CURRENT WEIGHT BASELINE")
    print(f"  Weights: {dict(zip(FACTOR_LABELS, CURRENT_WEIGHTS))}")
    baseline_bs = compute_baseline_brier(X, y, CURRENT_WEIGHTS)
    print(f"  Brier score: {baseline_bs:.4f}  (lower=better; naive=0.25)")

    # Logistic regression
    sep("LOGISTIC REGRESSION (L2, 5-fold CV)")
    importance, auc_scores, train_bs = run_logistic_regression(X, y)
    if importance is not None:
        print(f"  CV AUC   : {auc_scores.mean():.3f} +/- {auc_scores.std():.3f}")
        print(f"  Train Brier: {train_bs:.4f}")
        print()
        order = np.argsort(np.abs(importance))[::-1]
        print(f"  {'Factor':<18} {'Coef':>10}  Direction")
        print(f"  {'-'*18} {'-'*10}  ---------")
        for i in order:
            direction = "^ predicts WIN" if importance[i] > 0 else "v predicts LOSS"
            print(f"  {FACTOR_LABELS[i]:<18} {importance[i]:>10.4f}  {direction}")

        lr_weights = normalize_weights(np.abs(importance))
        print()
        print("  Implied weights (from logistic magnitude):")
        for label, w in zip(FACTOR_LABELS, lr_weights):
            print(f"    {label:<18}: {w:.1f}")

        # Bootstrap
        print()
        print("  Bootstrap CIs (n=1000)...")
        boot = bootstrap_importance(X, y, n_bootstrap=1000)
        if boot is not None:
            print(f"  {'Factor':<18}  {'Coef':>8}  {'95% CI':<20}  Reliable?")
            print(f"  {'-'*18}  {'-'*8}  {'-'*20}  ---------")
            for i, label in enumerate(FACTOR_LABELS):
                lo = np.percentile(boot[:, i], 2.5)
                hi = np.percentile(boot[:, i], 97.5)
                reliable = "YES (CI excludes 0)" if (lo > 0 or hi < 0) else "no"
                print(f"  {label:<18}  {importance[i]:>8.4f}  [{lo:>+7.3f}, {hi:>+7.3f}]  {reliable}")
    else:
        lr_weights = None

    # ROI optimization
    sep("ROI-OPTIMIZED WEIGHTS (differential evolution)")
    roi_raw = optimize_weights_roi(X, y, pnl)
    if roi_raw is not None:
        roi_weights = normalize_weights(roi_raw)
        print(f"  Optimized weights (maximize avg PnL on top-25% picks):")
        for label, w in zip(FACTOR_LABELS, roi_weights):
            cur = CURRENT_WEIGHTS[FACTOR_LABELS.index(label)]
            print(f"    {label:<18}: {w:>5.1f}  (was {cur:>2d}, delta {w-cur:>+.1f})")
    else:
        roi_weights = None

    # Final recommendation
    sep("FINAL RECOMMENDATION")

    if importance is not None and roi_weights is not None:
        blended = (normalize_weights(np.abs(importance)) / 100 * 50 +
                   roi_weights / 100 * 50)
        final_weights = normalize_weights(blended)
    elif roi_weights is not None:
        final_weights = roi_weights
    elif importance is not None:
        final_weights = normalize_weights(np.abs(importance))
    else:
        final_weights = np.array(CURRENT_WEIGHTS, dtype=float)

    rounded = np.round(final_weights).astype(int)
    diff = 100 - rounded.sum()
    if diff != 0:
        idx = np.argmax(final_weights - rounded) if diff > 0 else np.argmin(final_weights - rounded)
        rounded[idx] += diff

    print()
    print("  SUGGESTED WEIGHTS:")
    print()
    print(f"  {'Factor':<18}  {'Current':>7}  {'Suggested':>9}  {'Delta':>6}")
    print(f"  {'-'*18}  {'-'*7}  {'-'*9}  {'-'*6}")
    for label, cur, sug in zip(FACTOR_LABELS, CURRENT_WEIGHTS, rounded):
        delta = sug - cur
        flag = "  <- MAJOR" if abs(delta) >= 10 else ""
        print(f"  {label:<18}  {cur:>7}  {sug:>9}  {delta:>+6}{flag}")

    print()
    print("  Caveats:")
    print(f"    n={n} is borderline -- rerun at 200+ resolved for tighter estimates")
    print("    Selection bias: only bot-selected markets are in the dataset")
    print("    line_movement near-zero in almost all picks -- unreliable to calibrate")
    print("    Negative leaderboard signal may be confounded (market type, odds range)")
    print()

    if apply:
        sep("APPLYING TO .env")
        env_lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
        weight_map = {
            "WEIGHT_LEADERBOARD": int(rounded[0]),
            "WEIGHT_FAIR_VALUE": int(rounded[1]),
            "WEIGHT_LINE_MOVEMENT": int(rounded[2]),
            "WEIGHT_NEWS": int(rounded[3]),
            "WEIGHT_URGENCY": int(rounded[4]),
        }
        updated = {k: False for k in weight_map}
        new_lines = []
        for line in env_lines:
            key = line.split("=")[0].strip()
            if key in weight_map:
                new_lines.append(f"{key}={weight_map[key]}")
                updated[key] = True
            else:
                new_lines.append(line)
        for key, was_updated in updated.items():
            if not was_updated:
                new_lines.append(f"{key}={weight_map[key]}")
        ENV_PATH.write_text("\n".join(new_lines) + "\n")
        print(f"  Written to {ENV_PATH}")
        print()
        print("  Next: wire these into config.py + signal.py (see README).")
    else:
        print("  Run with --apply to write weights to .env")

    print()
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write optimized weights to .env")
    args = parser.parse_args()
    main(apply=args.apply)
