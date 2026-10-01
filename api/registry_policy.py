"""Pure registry status policy; no storage, network or wallet operations."""


def judgment_status(counts, readers=None, models=None, quorum=2):
    """Classify readings using trusted model identities supplied by the caller.

    ``models`` must contain only server-established model identities for the
    decisive readings, never client-declared names. Empty/unspecified identities
    do not count; key:/wallet: reader pseudonyms are never model identities.
    Legacy stored model names are unverified unless the caller independently
    establishes their provenance. The caller must filter them and deduplicate
    readings before invoking this helper.
    ``readers`` is retained for compatibility; distinct wallets authenticate
    signers, not independent reasoning. Wallet-only agreement remains
    ``read_once`` (pending), even after the numerical quorum is reached.
    Settlement requires quorum agreeing decisive readings and at least two
    distinct known trusted models. Agreement within one known model is
    ``agreed_same_model``; opposing decisive readings are always ``contested``.
    """
    if isinstance(quorum, bool) or not isinstance(quorum, int) or quorum < 1:
        raise ValueError("quorum must be a positive integer")
    asserts = counts.get("asserts", 0)
    denies = counts.get("does_not_assert", 0)
    if asserts and denies:
        return "contested"
    if not (asserts or denies or counts.get("partial", 0)):
        return "unread"
    if max(asserts, denies) < quorum:
        return "read_once"
    if isinstance(models, str):
        models = (models,)
    known = {
        model.strip() for model in (models or [])
        if isinstance(model, str) and model.strip()
        and model.strip().casefold() != "unspecified"
        and not model.strip().casefold().startswith(("key:", "wallet:"))
    }
    if len(known) > 1:
        return "confirmed_prior_art" if asserts else "ruled_out"
    if known:
        return "agreed_same_model"
    return "read_once"
