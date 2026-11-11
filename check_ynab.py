import sys

import ynab

from ynab_bot import get_ynab_metadata


def main():
    try:
        accounts, categories, payees, _ = get_ynab_metadata()
    except ynab.ApiException as exc:
        if exc.status == 401:
            print(
                "YNAB rejected YNAB_TOKEN. Create a new personal access token, "
                "update it in .env, and try again.",
                file=sys.stderr,
            )
        elif exc.status == 403:
            print(
                "YNAB denied access. Check that the token has access to this budget.",
                file=sys.stderr,
            )
        elif exc.status == 404:
            print(
                "YNAB could not find the configured budget. Check YNAB_BUDGET_ID.",
                file=sys.stderr,
            )
        else:
            print(
                f"YNAB connectivity check failed ({exc.status}): {exc.reason}",
                file=sys.stderr,
            )
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        "YNAB connection successful "
        f"({len(accounts)} accounts, {len(categories)} categories, "
        f"{len(payees)} payees)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
