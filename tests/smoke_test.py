"""End-to-end smoke test on the offline synthetic fixture (no GPU, no downloads).

Proves: data layer -> env reset/step -> verifiable reward -> eval harness all
wire together, and that the reward signal is *learnable* (a smart heuristic
beats random, oracle hits the ceiling).
"""
import sys
sys.path.insert(0, ".")

from rl_gym.recsys.env import RecSysRerankEnv
from rl_gym.recsys.reward import compute_reward, parse_ranking
from rl_gym.recsys.evaluate import (
    evaluate, random_policy, popularity_policy, oracle_policy,
    content_policy, item_cf_policy,
)
from rl_gym.recsys.data import get_episodes, format_prompt
from rl_gym.recsys.retrieval import build_catalog, LexicalRetriever
from rl_gym.recsys.inference import recommend


def test_reward_parsing():
    # valid permutation
    rb = compute_reward("RANKING: 2,0,1", [0, 0, 1])
    assert rb.valid and rb.parsed == [2, 0, 1]
    # clicked item (index 2) ranked first -> perfect NDCG
    assert rb.ndcg == 1.0, rb.ndcg
    # malformed -> invalid, zero reward
    bad = compute_reward("the best item is number two", [0, 0, 1])
    assert not bad.valid and bad.reward == 0.0
    # not a full permutation -> invalid
    assert parse_ranking("RANKING: 0,0,1", 3) == []
    print("  reward/parsing ........ OK")


def test_env_loop():
    env = RecSysRerankEnv(split="train")
    assert len(env) > 0
    obs, info = env.reset(index=0)
    assert "RANKING:" in obs and info["num_candidates"] > 0
    # feed a perfect (oracle) action and check reward is high
    ep = env.episodes[0]
    order = sorted(range(len(ep.labels)), key=lambda i: ep.labels[i], reverse=True)
    res = env.step("RANKING: " + ",".join(map(str, order)))
    assert res.terminated and res.reward > 0.9, res.reward
    print(f"  env reset/step ........ OK (oracle reward={res.reward:.3f})")


def test_eval_ordering():
    eps = get_episodes(split="dev")
    rnd = evaluate(random_policy(), eps)
    con = evaluate(content_policy(eps), eps)
    orc = evaluate(oracle_policy(eps), eps)
    print(f"  random  NDCG@10={rnd['NDCG@10']:.3f}  AUC={rnd['AUC']}")
    print(f"  content NDCG@10={con['NDCG@10']:.3f}  AUC={con['AUC']}")
    print(f"  oracle  NDCG@10={orc['NDCG@10']:.3f}  AUC={orc['AUC']}")
    # signal must be learnable: content (per-user history) beats random; oracle ceiling.
    # AUC is the discriminator here (NDCG@10 saturates with 12 candidates).
    assert con["AUC"] > rnd["AUC"], "content should beat random (AUC)"
    assert orc["NDCG@10"] > con["NDCG@10"], "oracle should be the ceiling"
    assert orc["NDCG@10"] > 0.99
    print("  eval ordering ......... OK (learnable signal confirmed)")


def test_retrieval_and_inference():
    eps = get_episodes(split="train")
    catalog = build_catalog(eps)
    assert len(catalog) > 0, "catalog should not be empty"
    retr = LexicalRetriever(catalog)

    # a user whose history is all one preferred category (synthetic fixture)
    ep = eps[0]
    pref = ep.history_titles[0].split()[0].lower()

    # retrieval alone should surface that preferred category
    cands = retr.retrieve(ep.history_titles, k=8)
    match = sum(1 for c in cands if c.category == pref)
    assert match >= len(cands) // 2, f"retrieval missed pref '{pref}': {[c.category for c in cands]}"

    # full two-stage pipeline (no ranker -> retrieval order) returns top_k
    items = recommend(ep.history_titles, retr, ranker=None, n_candidates=8, top_k=5)
    assert len(items) == 5
    print(f"  retrieval/inference ... OK (pref='{pref}', {match}/{len(cands)} top candidates on-topic)")


def test_steering_rewards():
    # all items relevant -> NDCG is 1.0 for any order, so reward differences come
    # purely from the *verifiable* steering constraints.
    labels = [1, 1, 1, 1]
    cats = ["sports", "sports", "tech", "food"]

    # diversity: a category-diverse top-k beats a same-category top-k
    diverse = compute_reward("RANKING: 0,2,3,1", labels, k=3, categories=cats, diversify=True)
    samey = compute_reward("RANKING: 0,1,2,3", labels, k=3, categories=cats, diversify=True)
    assert diverse.constraint > samey.constraint and diverse.reward >= samey.reward

    # exclude: surfacing an excluded category up top lowers reward vs no policy
    base = compute_reward("RANKING: 0,1,2,3", labels, k=4, categories=cats)
    excl = compute_reward("RANKING: 0,1,2,3", labels, k=4, categories=cats,
                          exclude_categories=("sports",))
    assert excl.reward < base.reward

    # recency: newer-first adheres better than older-first
    ages = [10, 0, 5, 1]
    new_first = compute_reward("RANKING: 1,3,2,0", labels, k=4, ages=ages, prefer_recent=True)
    old_first = compute_reward("RANKING: 0,2,3,1", labels, k=4, ages=ages, prefer_recent=True)
    assert new_first.constraint > old_first.constraint

    # the policy actually appears in the prompt the model is trained on
    eps = get_episodes(split="train", steering=True)
    steered = next(ep for ep in eps if ep.steering and ep.steering.text)
    assert "RANKING POLICY:" in format_prompt(steered)
    print("  steering rewards ...... OK (diversity/exclude/recency verifiable)")


def test_cold_start():
    train = get_episodes(split="train")
    dev = get_episodes(split="dev")
    train_ids = {c.news_id for ep in train for c in ep.candidates}
    dev_ids = {c.news_id for ep in dev for c in ep.candidates}
    assert train_ids.isdisjoint(dev_ids), "dev items must be unseen for a cold-start test"

    content = evaluate(content_policy(dev), dev)           # history match -> generalizes
    cf = evaluate(item_cf_policy(train, dev), dev)         # item memorization -> cold-blind
    print(f"  content AUC={content['AUC']:.3f}  (history match — generalizes to cold items)")
    print(f"  item-CF AUC={cf['AUC']:.3f}  (memorization — collapses on cold items)")
    assert content["AUC"] > cf["AUC"], "content must beat memorization on cold-start"
    print("  cold-start ............ OK (content generalizes, CF collapses)")


def test_chat_assistant():
    from rl_gym.recsys.assistant import ChatAssistant, RuleBackend, parse_steering
    from rl_gym.recsys.retrieval import build_catalog, LexicalRetriever

    eps = get_episodes(split="train")
    bot = ChatAssistant(retriever=LexicalRetriever(build_catalog(eps)),
                        backend=RuleBackend(), top_k=5)
    reply = bot.send("I love tech and finance, show me the latest")
    assert bot.last_items and len(bot.last_items) <= 5
    assert "picks for you" in reply.lower()

    # "latest" -> prefer_recent parsed, and the no-ranker path enforces newer-first
    assert parse_steering("show me the latest").prefer_recent
    ages = [it.age for it in bot.last_items]
    assert ages == sorted(ages), ages

    # a hard exclude filter is honored
    bot2 = ChatAssistant(retriever=LexicalRetriever(build_catalog(eps)),
                         backend=RuleBackend(), top_k=5)
    bot2.send("anything good, but no politics")
    assert all(it.category != "politics" for it in bot2.last_items)
    print("  chat assistant ........ OK (NL -> retrieve -> steered recommend -> reply)")


def test_user_routing():
    from rl_gym.recsys.assistant import ChatAssistant, RuleBackend, UserProfile
    from rl_gym.recsys.retrieval import build_catalog, LexicalRetriever

    retr = LexicalRetriever(build_catalog(get_episodes(split="train")))

    # cold: new user, no profile -> intent-only route
    cold = ChatAssistant(retriever=retr, backend=RuleBackend(), top_k=5)
    cold.send("recommend something")
    assert cold.last_route == "cold"

    # warm: returning user who likes sports -> sports surfaces on the SAME vague ask
    warm = ChatAssistant(retriever=retr, backend=RuleBackend(), top_k=5,
                         profile=UserProfile(user_id="u1",
                                             liked_titles=["Sports playoff game record"]))
    warm.send("recommend something")
    assert warm.last_route == "warm"
    assert any(it.category == "sports" for it in warm.last_items), \
        [it.category for it in warm.last_items]
    print("  user routing .......... OK (cold=intent-only, warm=profile-seeded)")


def test_chat_ui_respond():
    # the browser UI is a thin wrapper over respond(); verify the gradio-free path
    from rl_gym.recsys.chat_ui import respond
    history = [
        {"role": "user", "content": "I like sports"},
        {"role": "assistant", "content": "(earlier reply)"},
    ]
    reply = respond("show me the latest, no politics", history)
    assert isinstance(reply, str) and "picks for you" in reply.lower()
    print("  chat UI respond ....... OK (gradio-free data path)")


if __name__ == "__main__":
    print("Running smoke test on synthetic fixture...\n")
    test_reward_parsing()
    test_env_loop()
    test_eval_ordering()
    test_retrieval_and_inference()
    test_steering_rewards()
    test_cold_start()
    test_chat_assistant()
    test_user_routing()
    test_chat_ui_respond()
    print("\nALL SMOKE TESTS PASSED")
