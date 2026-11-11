import json
import os
import urllib.parse
import urllib.request

from dotenv import load_dotenv
from flask import Flask, jsonify, request
import ynab_bot

load_dotenv(override=True)

app = Flask(__name__)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
PENDING_TRANSACTIONS = {}
ACCOUNT_PAGE_SIZE = 8


def call_telegram_api(method, payload):
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set; skipping Telegram reply.")
        return None

    encoded_payload = urllib.parse.urlencode({
        key: value if key != "reply_markup" else json.dumps(value)
        for key, value in payload.items()
    }).encode()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    req = urllib.request.Request(url, data=encoded_payload, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {result.get('description', 'unknown error')}")
    return result.get("result")


def send_telegram_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return call_telegram_api("sendMessage", payload)


def edit_telegram_message(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return call_telegram_api("editMessageText", payload)


def answer_callback_query(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    call_telegram_api("answerCallbackQuery", payload)


def transaction_summary(resolved, account_label):
    payee_name = resolved["matched_payee_name"] or "(not specified)"
    if resolved["matched_payee_name"] and not resolved["matched_payee_id"]:
        payee_name += " (new payee)"
    parsed = resolved["parsed"]
    lines = [
        "Review transaction:",
        f"Payee: {payee_name}",
        f"Amount: {parsed.get('amount')}",
        f"Account: {account_label}",
        f"Category: {resolved['matched_category_name']}",
        f"Group: {resolved['matched_category_group_name'] or '(new group)'}",
    ]
    if parsed.get("memo"):
        lines.append(f"Memo: {parsed['memo']}")
    return "\n".join(lines)


def account_picker(resolved, page=0):
    accounts = resolved["account_options"]
    if not accounts:
        raise ValueError("No active YNAB accounts are available to select.")
    page_count = (len(accounts) + ACCOUNT_PAGE_SIZE - 1) // ACCOUNT_PAGE_SIZE
    page = max(0, min(page, page_count - 1))
    start = page * ACCOUNT_PAGE_SIZE
    page_accounts = accounts[start : start + ACCOUNT_PAGE_SIZE]
    suggested_id = resolved["matched_account_id"]

    text = transaction_summary(
        resolved,
        resolved["matched_account_name"] or "(choose an account)",
    )
    text += f"\n\nChoose the account to use (page {page + 1}/{page_count}):"
    rows = []
    for offset in range(0, len(page_accounts), 2):
        row = []
        for index, (name, account_id) in enumerate(
            page_accounts[offset : offset + 2], start=start + offset
        ):
            label = f"✓ {name}" if account_id == suggested_id else name
            if len(label) > 64:
                label = label[:61] + "..."
            row.append({
                "text": label,
                "callback_data": f"account:{index}",
            })
        rows.append(row)

    navigation = []
    if page > 0:
        navigation.append({
            "text": "← Previous",
            "callback_data": f"account-page:{page - 1}",
        })
    if page + 1 < page_count:
        navigation.append({
            "text": "Next →",
            "callback_data": f"account-page:{page + 1}",
        })
    if navigation:
        rows.append(navigation)
    rows.append([{"text": "Cancel", "callback_data": "cancel"}])
    return text, {"inline_keyboard": rows}


def confirmation_keyboard():
    return {
        "inline_keyboard": [[
            {"text": "Confirm and save", "callback_data": "confirm"},
            {"text": "Change account", "callback_data": "change-account"},
            {"text": "Cancel", "callback_data": "cancel"},
        ]]
    }


def saved_message(resolved):
    return (
        f"Saved to YNAB: {resolved['parsed'].get('amount')} from "
        f"{resolved['matched_account_name']} -> {resolved['matched_category_name']}"
    )


def handle_callback_query(callback_query):
    callback_id = callback_query.get("id")
    message = callback_query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    action = callback_query.get("data", "")
    pending = PENDING_TRANSACTIONS.get(chat_id)

    if not callback_id or not chat_id or not message_id or not pending:
        if callback_id:
            answer_callback_query(callback_id, "This transaction is no longer active.")
        return
    if pending.get("message_id") != message_id:
        answer_callback_query(callback_id, "This transaction is no longer active.")
        return

    if action == "cancel":
        PENDING_TRANSACTIONS.pop(chat_id, None)
        edit_telegram_message(chat_id, message_id, "Transaction cancelled.")
        answer_callback_query(callback_id, "Cancelled.")
        return

    if action.startswith("account-page:"):
        if pending["stage"] != "selecting":
            answer_callback_query(callback_id, "Choose Change account first.")
            return
        page = int(action.split(":", 1)[1])
        text, keyboard = account_picker(pending["resolved"], page)
        edit_telegram_message(chat_id, message_id, text, keyboard)
        answer_callback_query(callback_id)
        return

    if action.startswith("account:"):
        if pending["stage"] != "selecting":
            answer_callback_query(callback_id, "Choose Change account first.")
            return
        index = int(action.split(":", 1)[1])
        accounts = pending["resolved"]["account_options"]
        if index < 0 or index >= len(accounts):
            answer_callback_query(callback_id, "That account option is invalid.")
            return
        name, account_id = accounts[index]
        resolved = pending["resolved"]
        resolved["matched_account_name"] = name
        resolved["matched_account_id"] = account_id
        pending["stage"] = "confirming"
        edit_telegram_message(
            chat_id,
            message_id,
            transaction_summary(resolved, name) + "\n\nReply YES to save or NO to cancel.",
            confirmation_keyboard(),
        )
        answer_callback_query(callback_id, f"Selected {name}.")
        return

    if action == "change-account":
        if pending["stage"] != "confirming":
            answer_callback_query(callback_id, "Select an account first.")
            return
        pending["stage"] = "selecting"
        text, keyboard = account_picker(pending["resolved"])
        edit_telegram_message(chat_id, message_id, text, keyboard)
        answer_callback_query(callback_id)
        return

    if action == "confirm":
        if pending["stage"] != "confirming" or not pending["resolved"]["matched_account_id"]:
            answer_callback_query(callback_id, "Select an account before confirming.")
            return
        resolved = pending["resolved"]
        save_confirmed_transaction(resolved)
        PENDING_TRANSACTIONS.pop(chat_id, None)
        edit_telegram_message(chat_id, message_id, saved_message(resolved))
        answer_callback_query(callback_id, "Transaction saved.")
        return

    answer_callback_query(callback_id, "Unknown action.")


def preview_transaction(raw_text):
    """Parse and resolve a transaction without saving it yet."""
    accounts_map, categories_map, payees_map, category_groups_map = ynab_bot.get_ynab_metadata()
    parsed = ynab_bot.parse_and_match_transaction(
        raw_text,
        list(accounts_map.keys()),
        list(categories_map.keys()),
        list(category_groups_map.keys()),
    )
    resolved = ynab_bot.resolve_transaction_details(
        parsed, accounts_map, categories_map, payees_map, category_groups_map
    )
    resolved["account_options"] = sorted(accounts_map.items())
    return resolved


def save_confirmed_transaction(resolved):
    """Persist a previously parsed transaction into YNAB."""
    ynab_bot.push_to_ynab_via_sdk(
        resolved["parsed"],
        resolved["matched_account_id"],
        resolved["matched_category_id"],
        resolved["matched_payee_id"],
        resolved["matched_category_group_name"],
        resolved["matched_category_group_id"],
    )


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"})


@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json(silent=True) or {}
    callback_query = data.get("callback_query")
    if callback_query:
        callback_message = callback_query.get("message") or {}
        chat_id = (callback_message.get("chat") or {}).get("id")
        try:
            handle_callback_query(callback_query)
        except ynab_bot.ynab.ApiException as exc:
            print(f"YNAB API error ({exc.status}): {exc.reason}")
            if exc.status == 401:
                error_message = (
                    "YNAB rejected the access token. Create a new YNAB personal access "
                    "token, replace YNAB_TOKEN in .env, then restart the bot."
                )
            else:
                error_message = f"YNAB API request failed ({exc.status}): {exc.reason}"
            if callback_query.get("id"):
                answer_callback_query(callback_query["id"], error_message)
            if chat_id:
                send_telegram_message(chat_id, error_message)
        except Exception as exc:
            print(f"Error processing Telegram account selection: {exc}")
            if callback_query.get("id"):
                answer_callback_query(callback_query["id"], "Could not process that selection.")
            if chat_id:
                send_telegram_message(chat_id, f"Could not process account selection: {exc}")
        return jsonify({"status": "ok"})

    message = data.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = message.get("chat", {}).get("id")

    if not chat_id or not text:
        return jsonify({"status": "ok"})

    print(f"Received Telegram message: {text}")

    try:
        if text.lower().startswith("/start"):
            send_telegram_message(
                chat_id,
                "Send a transaction like: 50 on Starbucks using KBank Main. "
                "Choose an account, then confirm before saving.",
            )
            return jsonify({"status": "ok"})

        if chat_id in PENDING_TRANSACTIONS:
            pending = PENDING_TRANSACTIONS[chat_id]
            answer = text.lower()
            if answer in {"yes", "y", "confirm"}:
                if pending["stage"] != "confirming":
                    send_telegram_message(chat_id, "Please select an account using the buttons first.")
                    return jsonify({"status": "ok"})
                resolved = pending["resolved"]
                save_confirmed_transaction(resolved)
                PENDING_TRANSACTIONS.pop(chat_id, None)
                send_telegram_message(chat_id, saved_message(resolved))
            elif answer in {"no", "n", "cancel", "cancelled"}:
                PENDING_TRANSACTIONS.pop(chat_id, None)
                send_telegram_message(chat_id, "Transaction cancelled.")
            else:
                if pending["stage"] == "selecting":
                    send_telegram_message(chat_id, "Please select an account using the buttons.")
                else:
                    send_telegram_message(
                        chat_id,
                        "Please reply YES to save this transaction or NO to cancel.",
                    )
            return jsonify({"status": "ok"})

        resolved = preview_transaction(text)
        if not resolved["matched_category_name"]:
            raise ValueError("The AI could not determine a valid category name for this transaction.")
        if not (resolved["matched_category_id"] or resolved["matched_category_group_name"]):
            raise ValueError("The AI could not determine a valid category group for this transaction.")

        PENDING_TRANSACTIONS[chat_id] = {
            "resolved": resolved,
            "stage": "selecting",
        }
        selection_text, keyboard = account_picker(resolved)
        sent_message = send_telegram_message(chat_id, selection_text, keyboard)
        if sent_message:
            PENDING_TRANSACTIONS[chat_id]["message_id"] = sent_message.get("message_id")
    except ynab_bot.ynab.ApiException as exc:
        print(f"YNAB API error ({exc.status}): {exc.reason}")
        if exc.status == 401:
            error_message = (
                "YNAB rejected the access token. Create a new YNAB personal access "
                "token, replace YNAB_TOKEN in .env, then restart the bot."
            )
        else:
            error_message = f"YNAB API request failed ({exc.status}): {exc.reason}"
        send_telegram_message(chat_id, error_message)
    except Exception as exc:
        print(f"Error processing transaction: {exc}")
        send_telegram_message(chat_id, f"Could not parse transaction: {exc}")

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host=HOST, port=PORT)
