"""End-to-end smoke test for the meal-planning vertical (offline, no GPU/downloads).

Proves: data layer -> calculable verifiable reward -> eval harness wire together,
the dietary gate is hard, and the reward signal is *learnable* (oracle >> random).
"""
import sys
sys.path.insert(0, ".")

from rl_gym.meals.data import get_meal_episodes, format_meal_prompt, make_synthetic_meals
from rl_gym.meals.reward import compute_meal_reward, parse_plan, diet_ok, _saturate
from rl_gym.meals.evaluate import random_plan, oracle_plan, evaluate_meals
from rl_gym.meals.audit import sampled_audit
from rl_gym.meals.s3io import materialize, upload_dir, download_dir


def test_parse_plan():
    assert parse_plan("PLAN: 0,3,5,7", 12, 4) == [0, 3, 5, 7]
    assert parse_plan("PLAN: 0,3,5", 12, 4) == []           # wrong count
    assert parse_plan("PLAN: 0,0,5,7", 12, 4) == []          # duplicate
    assert parse_plan("PLAN: 0,3,5,99", 12, 4) == []         # out of range
    assert parse_plan("just pick the curry", 12, 4) == []    # malformed
    print("  parse_plan ............ OK")


def test_reward_and_gate():
    eps = make_synthetic_meals(n_episodes=5, seed=0)
    ep = eps[0]
    # oracle plan should score high and satisfy the dietary gate
    rb = compute_meal_reward(oracle_plan(ep), ep.request, ep.candidates)
    assert rb.valid and rb.dietary_ok, rb
    assert rb.reward > 0.8, rb.reward

    # a plan containing a dietary-violating recipe is hard-gated to 0
    req = ep.request
    bad = next((i for i, r in enumerate(ep.candidates) if not diet_ok(r, req)), None)
    if bad is not None:
        others = [i for i in range(len(ep.candidates)) if i != bad][: req.n_meals - 1]
        rb_bad = compute_meal_reward("PLAN: " + ",".join(map(str, [bad] + others)),
                                     req, ep.candidates)
        assert rb_bad.valid and not rb_bad.dietary_ok and rb_bad.reward == 0.0, rb_bad
    print(f"  reward + dietary gate . OK (oracle reward={rb.reward:.3f})")


def test_learnable_signal():
    eps = get_meal_episodes(split="dev")
    rnd = evaluate_meals(random_plan(), eps)
    orc = evaluate_meals(oracle_plan, eps)
    print(f"  random  reward={rnd['reward']:.3f}  dietary_ok={rnd['dietary_ok_rate']:.2f}")
    print(f"  oracle  reward={orc['reward']:.3f}  dietary_ok={orc['dietary_ok_rate']:.2f}")
    assert orc["reward"] > rnd["reward"] + 0.2, "oracle must clearly beat random"
    assert orc["dietary_ok_rate"] > 0.99, "oracle must always satisfy the diet gate"
    print("  learnable signal ...... OK (oracle >> random)")


def test_prompt():
    ep = get_meal_episodes(split="train")[0]
    pr = format_meal_prompt(ep)
    assert "PLAN:" in pr and "CONSTRAINTS:" in pr and "[0]" in pr
    print("  meal prompt ........... OK")


def test_saturate():
    # the transform itself: identity at s>=1, renormalised + clamped below
    assert _saturate(0.4, 1.0) == 0.4
    assert abs(_saturate(0.4, 0.5) - 0.8) < 1e-9     # below cap: 0.4/0.5
    assert _saturate(0.9, 0.5) == 1.0                 # above cap: clamped
    assert _saturate(0.0, 0.5) == 0.0

    eps = make_synthetic_meals(n_episodes=5, seed=0)
    ep = eps[0]
    plan = oracle_plan(ep)
    base = compute_meal_reward(plan, ep.request, ep.candidates)
    sat1 = compute_meal_reward(plan, ep.request, ep.candidates, saturate=1.0)
    low = compute_meal_reward(plan, ep.request, ep.candidates, saturate=0.6)
    assert base.reward == sat1.reward                 # default is a no-op
    # raw component breakdown is preserved regardless of saturation (audit honesty)
    for f in ("nutrition", "budget", "time", "variety", "palatability"):
        assert getattr(base, f) == getattr(low, f)
    assert 0.0 <= low.reward <= 1.0
    # saturation never bypasses the hard dietary gate
    bad = next((i for i, r in enumerate(ep.candidates) if not diet_ok(r, ep.request)), None)
    if bad is not None:
        others = [i for i in range(len(ep.candidates)) if i != bad][: ep.request.n_meals - 1]
        rb = compute_meal_reward("PLAN: " + ",".join(map(str, [bad] + others)),
                                 ep.request, ep.candidates, saturate=0.6)
        assert rb.reward == 0.0
    print("  reward saturation ..... OK (no-op at 1.0, raw preserved, gate intact)")


def test_sampled_audit():
    eps = make_synthetic_meals(n_episodes=5, seed=0)
    det = sampled_audit(oracle_plan, eps, g=8)        # deterministic selector
    sto = sampled_audit(random_plan(), eps, g=8)      # stochastic selector
    # a deterministic policy has no within-episode spread → the collapse signature
    assert det["reward_mean_std"] == 0.0
    assert det["within_unique"] <= 1.0 / 8 + 1e-9     # one distinct plan per episode
    # a stochastic policy explores → std and within-uniqueness both rise
    assert sto["reward_mean_std"] > 0.0
    assert sto["within_unique"] > det["within_unique"]
    assert det["invalid_rate"] == 0.0 and sto["invalid_rate"] == 0.0
    print(f"  sampled degeneracy .... OK (det std={det['reward_mean_std']:.3f} "
          f"< stochastic std={sto['reward_mean_std']:.3f})")


def test_foodcom_loader():
    # exercise the real loader on a tiny in-format CSV fixture (no download)
    import csv as _csv, os, tempfile
    from rl_gym.meals.data import load_foodcom

    d = tempfile.mkdtemp()
    rec = os.path.join(d, "RAW_recipes.csv")
    inter = os.path.join(d, "RAW_interactions.csv")
    with open(rec, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["name", "id", "minutes", "contributor_id", "submitted",
                    "tags", "nutrition", "n_steps", "steps", "description",
                    "ingredients", "n_ingredients"])
        # calories, fat%, sugar%, sodium%, protein%, satfat%, carbs%
        w.writerow(["veg curry", "1", "25", "0", "2020",
                    "['vegetarian', 'indian']", "[520.0, 10, 5, 8, 40, 6, 30]",
                    "3", "[]", "tasty", "['chickpeas', 'coconut milk', 'rice']", "3"])
        w.writerow(["beef stew", "2", "90", "0", "2020",
                    "['american']", "[700.0, 20, 4, 12, 60, 10, 20]",
                    "5", "[]", "hearty", "['beef', 'potato', 'carrot']", "3"])
        w.writerow(["nutty salad", "3", "15", "0", "2020",
                    "['vegetarian']", "[300.0, 8, 6, 5, 20, 3, 18]",
                    "2", "[]", "fresh", "['walnut', 'lettuce', 'olive oil']", "3"])
    with open(inter, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["user_id", "recipe_id", "date", "rating", "review"])
        w.writerow(["u1", "1", "2020", "5", "great"])
        w.writerow(["u2", "1", "2020", "4", "good"])

    eps = load_foodcom(rec, inter, n_episodes=5, pool_size=3, seed=0)
    assert len(eps) == 5
    by_id = {r.recipe_id: r for ep in eps for r in ep.candidates}
    curry, beef, salad = by_id["1"], by_id["2"], by_id["3"]
    assert curry.kcal == 520 and curry.vegetarian and not curry.contains_nuts
    assert curry.protein_g == 20            # 40% PDV * 50g / 100
    assert curry.avg_rating == 4.5          # (5+4)/2 from interactions
    assert not beef.vegetarian              # 'american' tag, no veg tag
    assert salad.contains_nuts              # walnut in ingredients
    assert beef.prep_min == 90 and curry.prep_min == 25
    print("  food.com loader ....... OK (nutrition + diet + allergen + ratings parsed)")


def test_s3io_materialize():
    # materialize() only acts on s3:// URIs; local paths and HF ids pass through
    # untouched (so loaders work the same whether or not S3 is configured).
    assert materialize("./out/meals-grpo") == "./out/meals-grpo"
    assert materialize("Qwen/Qwen2.5-1.5B-Instruct") == "Qwen/Qwen2.5-1.5B-Instruct"
    assert callable(upload_dir) and callable(download_dir)   # save/load interface present
    print("  s3io materialize ...... OK (s3:// load opt-in; local/HF paths untouched)")


def test_gym_reward_matches():
    # the general gym Reward (Components + Gates) must reproduce the validated
    # meal reward exactly — proves the abstraction is faithful, not a fork.
    from rl_gym.meals.env import MealsEnv
    from rl_gym.gym.core import score_completion
    env = MealsEnv()
    for ep in make_synthetic_meals(n_episodes=25, seed=3):
        for comp in (oracle_plan(ep), random_plan(7)(ep), "garbage, no plan here"):
            a = compute_meal_reward(comp, ep.request, ep.candidates).reward
            b = score_completion(env, comp, ep).reward
            assert abs(a - b) < 1e-9, (a, b, comp[:24])
    print("  gym Reward matches ..... OK (MealsEnv == compute_meal_reward)")


def test_gym_pipeline():
    # the env-generic eval + GRPO data layer must reproduce the meal path exactly.
    from rl_gym.gym.registry import get_env
    from rl_gym.gym.eval import evaluate_policy
    from rl_gym.gym.data import build_dataset, make_reward_fn
    from rl_gym.gym.core import score_completion
    env = get_env("meals")                         # synthetic fixture (data_dir=None)
    eps = env.episodes("dev")
    # generic eval == meal eval on the same episodes (deterministic oracle policy)
    a = evaluate_policy(env, lambda ep: env.oracle(ep), eps)["reward"]
    b = evaluate_meals(oracle_plan, eps)["reward"]
    assert abs(a - b) < 1e-9, (a, b)
    # generic GRPO reward fn (ep_id closure) == score_completion, row-for-row
    ds, train_eps = build_dataset(env, "train", limit=8)
    comps = [env.oracle(train_eps[i]) for i in ds["ep_id"]]
    got = make_reward_fn(env, train_eps)(ds["prompt"], comps, ds["ep_id"])
    exp = [score_completion(env, c, train_eps[i]).reward for c, i in zip(comps, ds["ep_id"])]
    assert got == exp and len(got) == len(ds["ep_id"])
    print("  gym pipeline .......... OK (env-generic eval + GRPO reward fn == meals)")


def test_serve_plan():
    # the /plan logic (mock policies, no model download) returns the right shape and
    # shows the contrast: rating-greedy base trips the gate; oracle-grpo passes.
    from rl_gym.gym.serve import CURATED_POOL, run_plan, _heuristic
    from rl_gym.gym.registry import get_env
    env = get_env("meals")
    bp, bl = _heuristic(env, "base")
    gp, gl = _heuristic(env, "grpo")
    state = {"env": env, "pool": CURATED_POOL,
             "pol": {"base": bp, "grpo": gp}, "lab": {"base": bl, "grpo": gl}}
    out = run_plan({"vegetarian": True, "exclude_nuts": True,
                    "kcal_target": 600, "budget_per_serving": 6}, state)
    assert len(out["candidates"]) == len(CURATED_POOL)
    r = out["results"]
    assert r["grpo"]["gate"] and not r["base"]["gate"]        # gate is the differentiator
    assert r["grpo"]["reward"] > r["base"]["reward"]
    assert set(r["grpo"]["components"]) == {"nutrition", "budget", "time", "variety", "palatability"}
    print("  serve /plan ........... OK (base gate-fail < grpo gate-pass; breakdown present)")


if __name__ == "__main__":
    print("Running meal-planning smoke test on synthetic fixture...\n")
    test_parse_plan()
    test_reward_and_gate()
    test_learnable_signal()
    test_prompt()
    test_saturate()
    test_sampled_audit()
    test_s3io_materialize()
    test_gym_reward_matches()
    test_gym_pipeline()
    test_serve_plan()
    test_foodcom_loader()
    print("\nALL MEAL SMOKE TESTS PASSED")
