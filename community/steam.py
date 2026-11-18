import re

STEAM_ID64_BASE = 76561197960265728
MAX_ACCOUNT_ID = (1 << 32) - 1


def steam_id_variants(value):
    value = value.strip().upper()
    steam2 = re.fullmatch(r"STEAM_[01]:([01]):([0-9]{1,10})", value)
    steam3 = re.fullmatch(r"\[U:1:([0-9]{1,10})\]", value)
    if steam2:
        account_id = 2 * int(steam2[2]) + int(steam2[1])
    elif steam3:
        account_id = int(steam3[1])
    elif re.fullmatch(r"[0-9]{17}", value):
        account_id = int(value) - STEAM_ID64_BASE
    else:
        return ()
    if not 0 < account_id <= MAX_ACCOUNT_ID:
        return ()
    suffix = f"{account_id % 2}:{account_id // 2}"
    return f"STEAM_0:{suffix}", f"STEAM_1:{suffix}", f"[U:1:{account_id}]", str(STEAM_ID64_BASE + account_id)
