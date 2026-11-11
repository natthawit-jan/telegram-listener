import json
import math
import os
import re
from datetime import date

import ynab
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(override=True)

# Configuration
YNAB_TOKEN = os.environ.get("YNAB_TOKEN", "")
BUDGET_ID = os.environ.get("YNAB_BUDGET_ID", "")
client = OpenAI(
    base_url=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1"),
    api_key=os.environ.get("OPENAI_API_KEY", "not-needed"),
)
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "apple-foundationmodel")


def require_ynab_settings():
  if not YNAB_TOKEN or not BUDGET_ID:
    raise RuntimeError("Set YNAB_TOKEN and YNAB_BUDGET_ID in .env.")


def get_ynab_metadata():
  """Fetches accounts, categories, and payees from YNAB using the official SDK."""
  require_ynab_settings()
  configuration = ynab.Configuration(access_token=YNAB_TOKEN)
  with ynab.ApiClient(configuration) as api_client:
    # 1. Fetch Accounts
    accounts_api = ynab.AccountsApi(api_client)
    acc_response = accounts_api.get_accounts(BUDGET_ID)
    accounts = {acc.name: acc.id for acc in acc_response.data.accounts if not acc.closed and acc.on_budget and acc.type in ["cash", "savings", "checking"]}
    # 2. Fetch Categories
    categories_api = ynab.CategoriesApi(api_client)
    cat_response = categories_api.get_categories(BUDGET_ID)
    categories = {}
    category_groups = {}
    for group in cat_response.data.category_groups:
      if group.hidden or group.deleted or group.internal:
        continue
      category_groups[group.name] = group.id
      for cat in group.categories:
        if not cat.hidden and not cat.deleted:
          categories[f"{group.name} / {cat.name}"] = cat.id

    # 3. Fetch Payees
    payees_api = ynab.PayeesApi(api_client)
    payee_response = payees_api.get_payees(BUDGET_ID)
    payees = {p.name: p.id for p in payee_response.data.payees if not p.deleted}

    return accounts, categories, payees, category_groups


def parse_and_match_transaction(
    raw_text: str,
    available_accounts: list,
    available_categories: list,
    available_category_groups: list,
):
  """Extracts transaction details and matches or proposes YNAB entities."""
  accounts_list_str = ", ".join([f"'{acc}'" for acc in available_accounts])
  categories_list_str = ", ".join([f"'{cat}'" for cat in available_categories])
  category_groups_list_str = ", ".join(
      [f"'{group}'" for group in available_category_groups]
  )

  prompt = f"""
    You are an intelligent financial transaction parser.
    Analyze the following text and extract:
    1. "raw_payee": string (the exact merchant or recipient named in the text; do not substitute a different existing payee. For example, in "50 baht for Cafe Amazon", return "Cafe Amazon".)
    2. "amount": float (positive number)
    3. "memo": string (brief description or empty string)
    4. "target_account": string (Choose an exact account name from this list only when the user explicitly identifies that account. Never guess or choose the closest match. If the account is unclear, return an empty string.)
    5. "existing_category": string (Use the exact "Group / Category" label from [{categories_list_str}] ONLY when it directly and clearly matches what was purchased and the user did not request a different category. Otherwise return an empty string.)
    6. "new_category_name": string (If existing_category is empty, use the category the user explicitly requested, correcting capitalization/wording if needed; otherwise suggest a concise category name suitable for the purchase. Otherwise return an empty string.)
    7. "target_category_group": string (If creating a category, honor the group explicitly named by the user, including a group that does not exist yet. Otherwise choose the most suitable existing group from [{category_groups_list_str}], or propose a concise new group name if none fits. For an existing category, return an empty string.)

    Critical category-group rule: category group must be the parent/superset of the category, not the same thing. The category and group names must not be identical. For example, for eyeglasses, use a category like "Eyewear" and a parent group like "Health" or "Personal Accessories"; do NOT use "Eyewear" as both the category and group.
    Another example: if the item is a video game, use "Video Games" as the category and a broad group like "Entertainment" or "Hobbies"; do not set the group equal to "Video Games".
    If the category is generic or a leaf-level label, choose a broader parent bucket for the group.

    Never choose an existing category merely because it is the closest available option; it must fit the purchase's meaning.
    Example: for "iphone 18 pro 34,000 baht on kbank category is electronic under Apple", return new_category_name "Electronics" and target_category_group "Apple", even if Apple is not in the existing groups list.
    Return ONLY a valid JSON object with those 7 keys. When existing_category is empty, always provide a non-empty new_category_name and target_category_group. Do not include a date or choose a payee from a list.

    Text: "{raw_text}"
    """

  response = client.chat.completions.create(
      model=OPENAI_MODEL,
      messages=[{"role": "user", "content": prompt}],
      temperature=0.0,
  )

  content = response.choices[0].message.content.strip()
  if content.startswith("```"):
    content = content.split("```")[1]
    if content.startswith("json"):
      content = content[4:]
  transaction = json.loads(content.strip())
  transaction["target_account"] = match_account_from_text(
      raw_text, available_accounts
  ) or ""
  existing_category = transaction.get("existing_category", "").strip()
  new_category_name = transaction.get("new_category_name", "").strip()
  transaction["target_category"] = existing_category or new_category_name
  transaction["date"] = date.today().isoformat()
  return transaction


def edit_transaction_details(
    instruction: str,
    resolved: dict,
    accounts_map: dict,
    categories_map: dict,
    payees_map: dict,
    category_groups_map: dict,
):
  """Apply only the transaction fields explicitly changed by a follow-up."""
  parsed = resolved["parsed"]
  current_details = {
      "payee": parsed.get("raw_payee", ""),
      "amount": parsed.get("amount"),
      "memo": parsed.get("memo", ""),
      "account": resolved.get("matched_account_name"),
      "category": resolved.get("matched_category_name"),
      "category_group": resolved.get("matched_category_group_name"),
  }
  prompt = f"""
    Update a pending financial transaction based only on the user's correction.
    Current transaction: {json.dumps(current_details)}

    Available account names: {json.dumps(list(accounts_map))}
    Available category labels (format "Group / Category"): {json.dumps(list(categories_map))}
    Available category groups: {json.dumps(list(category_groups_map))}

    Return one JSON object with exactly these keys:
    "raw_payee", "amount", "memo", "target_account", "target_category",
    "target_category_group".
    Set a value to null unless the user explicitly asks to change that field.
    For payee and memo, return strings; an empty string clears the field.
    Amount must be a positive number.
    For target_account, use an exact available account name.
    For target_category, use an exact available "Group / Category" label if one fits;
    otherwise use the requested new category name. If creating a category, also
    provide its parent target_category_group, using an available group or the
    explicitly requested new group. A category group alone cannot be changed.
    Do not infer changes from the current transaction or modify unmentioned fields.
    Return JSON only.

    User correction: {json.dumps(instruction)}
  """
  response = client.chat.completions.create(
      model=OPENAI_MODEL,
      messages=[{"role": "user", "content": prompt}],
      temperature=0.0,
  )
  content = response.choices[0].message.content.strip()
  if content.startswith("```"):
    content = content.split("```")[1]
    if content.startswith("json"):
      content = content[4:]
  changes = json.loads(content.strip())
  if not isinstance(changes, dict):
    raise ValueError("The model did not return a JSON object for the correction.")
  allowed_fields = {
      "raw_payee",
      "amount",
      "memo",
      "target_account",
      "target_category",
      "target_category_group",
  }
  if set(changes) - allowed_fields:
    raise ValueError("The model returned unknown fields for the correction.")

  updated_parsed = parsed.copy()
  payee = changes.get("raw_payee")
  if payee is not None:
    if not isinstance(payee, str):
      raise ValueError("The corrected payee must be text.")
    updated_parsed["raw_payee"] = payee.strip()

  amount = changes.get("amount")
  if amount is not None:
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
      raise ValueError("The corrected amount must be a positive number.")
    if not math.isfinite(amount) or amount <= 0:
      raise ValueError("The corrected amount must be a positive number.")
    updated_parsed["amount"] = amount

  memo = changes.get("memo")
  if memo is not None:
    if not isinstance(memo, str):
      raise ValueError("The corrected memo must be text.")
    updated_parsed["memo"] = memo.strip()

  target_account = changes.get("target_account")
  account_was_changed = target_account is not None
  if account_was_changed:
    if not isinstance(target_account, str):
      raise ValueError("The corrected account must be an available account name.")
    matched_account = next(
        (
            name
            for name in accounts_map
            if name.casefold() == target_account.strip().casefold()
        ),
        None,
    )
    if not matched_account:
      raise ValueError("The corrected account is not in the available YNAB accounts.")
    updated_parsed["target_account"] = matched_account

  target_category = changes.get("target_category")
  target_category_group = changes.get("target_category_group")
  if target_category is not None:
    if not isinstance(target_category, str) or not target_category.strip():
      raise ValueError("The corrected category must not be empty.")
    target_category = target_category.strip()
    existing_category = next(
        (
            label
            for label in categories_map
            if label.casefold() == target_category.casefold()
        ),
        None,
    )
    if existing_category:
      parent_group = existing_category.rsplit(" / ", 1)[0]
      updated_parsed["existing_category"] = existing_category
      updated_parsed["new_category_name"] = ""
      updated_parsed["target_category"] = existing_category
      updated_parsed["target_category_group"] = parent_group
    else:
      if not isinstance(target_category_group, str) or not target_category_group.strip():
        raise ValueError("A parent group is required when creating a new category.")
      updated_parsed["existing_category"] = ""
      updated_parsed["new_category_name"] = target_category
      updated_parsed["target_category"] = target_category
      updated_parsed["target_category_group"] = target_category_group.strip()
  elif target_category_group is not None:
    raise ValueError("Specify a category along with its parent group.")

  updated = resolve_transaction_details(
      updated_parsed,
      accounts_map,
      categories_map,
      payees_map,
      category_groups_map,
  )
  if not account_was_changed:
    updated["matched_account_name"] = resolved["matched_account_name"]
    updated["matched_account_id"] = resolved["matched_account_id"]
  updated["account_options"] = resolved["account_options"]
  updated["suggested_account_id"] = resolved["suggested_account_id"]
  updated["edit_context"] = resolved["edit_context"]
  return updated, account_was_changed


def match_account_from_text(raw_text, available_accounts):
  """Match an explicitly named account, unique name-token match, or number fragment."""
  normalized_text = re.sub(r"[^a-z0-9]+", " ", raw_text.casefold()).strip()
  named_matches = [
      account_name
      for account_name in available_accounts
      if re.sub(r"[^a-z0-9]+", " ", account_name.casefold()).strip()
      in normalized_text
  ]
  if len(named_matches) == 1:
    return named_matches[0]
  if named_matches:
    return None

  message_tokens = set(re.findall(r"[a-z]+", normalized_text))
  token_scores = {
      account_name: len(
          set(re.findall(r"[a-z]+", account_name.casefold())) & message_tokens
      )
      for account_name in available_accounts
  }
  highest_score = max(token_scores.values(), default=0)
  best_matches = [
      account_name
      for account_name, score in token_scores.items()
      if score == highest_score and score > 0
  ]
  if len(best_matches) == 1:
    return best_matches[0]
  if best_matches:
    return None

  account_number = re.search(
      r"\b(?:account|acct)\s*(?:number|no\.?|#)?\s*(\d{4,})\b",
      raw_text,
      re.IGNORECASE,
  )
  if not account_number:
    return None

  identifier = account_number.group(1)
  number_matches = [
      account_name
      for account_name in available_accounts
      if identifier in re.sub(r"\D", "", account_name)
  ]
  return number_matches[0] if len(number_matches) == 1 else None


def find_category_id(category_name, categories_map):
  """Finds an exact category label, or an unambiguous category name."""
  if not category_name:
    return None

  normalized_name = category_name.strip().casefold()
  for label, category_id in categories_map.items():
    if label.casefold() == normalized_name:
      return category_id

  matching_ids = {
      category_id
      for label, category_id in categories_map.items()
      if label.rsplit(" / ", 1)[-1].strip().casefold() == normalized_name
  }
  return next(iter(matching_ids)) if len(matching_ids) == 1 else None


def push_to_ynab_via_sdk(
    transaction_data,
    account_id,
    category_id,
    payee_id,
    category_group_name,
    category_group_id,
):
  """Creates missing payees/categories, then pushes the transaction to YNAB."""
  require_ynab_settings()
  configuration = ynab.Configuration(access_token=YNAB_TOKEN)

  with ynab.ApiClient(configuration) as api_client:
    transactions_api = ynab.TransactionsApi(api_client)
    payee_name = transaction_data.get("raw_payee")

    if not category_id:
      category_name = transaction_data.get("target_category")
      if not category_name:
        raise ValueError("A category name is required to create a new category.")
      if not category_group_id:
        if not category_group_name:
          raise ValueError(
              f"Cannot create category '{category_name}': "
              "the model did not provide a category group name."
          )
        categories_api = ynab.CategoriesApi(api_client)
        group_wrapper = ynab.PostCategoryGroupWrapper(
            category_group=ynab.SaveCategoryGroup(name=category_group_name)
        )
        category_group_id = (
            categories_api.create_category_group(BUDGET_ID, group_wrapper)
            .data.category_group.id
        )
        print(f"Created new category group: {category_group_name}")
      categories_api = ynab.CategoriesApi(api_client)
      categories_response = categories_api.get_categories(BUDGET_ID)
      existing_category = next(
          (
              category
              for group in categories_response.data.category_groups
              if group.id == category_group_id
              for category in group.categories
              if not category.hidden
              and not category.deleted
              and category.name.casefold() == category_name.strip().casefold()
          ),
          None,
      )
      if existing_category:
        category_id = existing_category.id
        print(
            f"Using existing category: "
            f"{category_group_name} / {existing_category.name}"
        )
      else:
        category_wrapper = ynab.PostCategoryWrapper(
            category=ynab.NewCategory(
                name=category_name,
                category_group_id=category_group_id,
            )
        )
        category_id = (
            categories_api.create_category(BUDGET_ID, category_wrapper)
            .data.category.id
        )
        print(f"Created new category: {category_name} ({category_group_name})")

    if not payee_id:
      if not payee_name:
        raise ValueError("A payee name is required to create a new payee.")
      payees_api = ynab.PayeesApi(api_client)
      payee_wrapper = ynab.PostPayeeWrapper(
          payee=ynab.PostPayee(name=payee_name)
      )
      payee_id = payees_api.create_payee(BUDGET_ID, payee_wrapper).data.payee.id
      print(f"Created new payee: {payee_name}")

    # YNAB API requires amounts in milliunits (e.g., $53.00 = -53000 for outflow)
    milliunit_amount = int(float(transaction_data["amount"]) * -1000)

    new_transaction = ynab.NewTransaction(
        account_id=account_id,
        category_id=category_id,
        payee_id=payee_id,
        date=transaction_data["date"],
        amount=milliunit_amount,
        memo=transaction_data.get("memo", ""),
        cleared=ynab.TransactionClearedStatus.CLEARED,
        approved=False,
    )

    wrapper = ynab.PostTransactionsWrapper(transaction=new_transaction)

    transactions_api.create_transaction(BUDGET_ID, wrapper)
    print("✅ Successfully saved transaction with payee, account, and category to YNAB!")


def resolve_transaction_details(parsed, accounts_map, categories_map, payees_map, category_groups_map):
  """Resolve the model output into exact YNAB account/category IDs."""
  matched_account_name = parsed.get("target_account")
  matched_account_id = accounts_map.get(matched_account_name)

  existing_category_name = parsed.get("existing_category")
  matched_category_name = parsed.get("target_category")
  matched_category_id = find_category_id(existing_category_name, categories_map)
  if not matched_category_id and matched_category_name:
    matched_category_id = find_category_id(matched_category_name, categories_map)

  matched_payee_name = parsed.get("raw_payee")
  matched_payee_id = payees_map.get(matched_payee_name)
  if not matched_payee_id:
    matched_payee_id = next(
        (
            payee_id
            for name, payee_id in payees_map.items()
            if name.casefold() == (matched_payee_name or "").casefold()
        ),
        None,
    )

  matched_category_group_name = parsed.get("target_category_group")
  matched_category_group_id = category_groups_map.get(matched_category_group_name)
  if not matched_category_group_id:
    matched_category_group_id = next(
        (
            group_id
            for name, group_id in category_groups_map.items()
            if name.casefold()
            == (matched_category_group_name or "").casefold()
        ),
        None,
    )
  if matched_category_group_id:
    matched_category_group_name = next(
        name
        for name, group_id in category_groups_map.items()
        if group_id == matched_category_group_id
    )

  return {
      "parsed": parsed,
      "matched_account_name": matched_account_name,
      "matched_account_id": matched_account_id,
      "matched_category_name": matched_category_name,
      "matched_category_id": matched_category_id,
      "matched_payee_name": matched_payee_name,
      "matched_payee_id": matched_payee_id,
      "matched_category_group_name": matched_category_group_name,
      "matched_category_group_id": matched_category_group_id,
  }


def process_transaction_text(raw_input_text):
  """Fetch YNAB metadata, parse a transaction, resolve matches, and save it."""
  accounts_map, categories_map, payees_map, category_groups_map = get_ynab_metadata()
  parsed = parse_and_match_transaction(
      raw_input_text,
      list(accounts_map.keys()),
      list(categories_map.keys()),
      list(category_groups_map.keys()),
  )
  resolved = resolve_transaction_details(
      parsed, accounts_map, categories_map, payees_map, category_groups_map
  )

  if not resolved["matched_account_id"]:
    raise ValueError(
        f"Could not map the transaction to a valid account from: {sorted(accounts_map.keys())}"
    )
  if not resolved["matched_category_name"]:
    raise ValueError("The AI could not determine a valid category name for the transaction.")
  if not (resolved["matched_category_id"] or resolved["matched_category_group_name"]):
    raise ValueError("The AI could not determine a valid category group for the transaction.")

  push_to_ynab_via_sdk(
      resolved["parsed"],
      resolved["matched_account_id"],
      resolved["matched_category_id"],
      resolved["matched_payee_id"],
      resolved["matched_category_group_name"],
      resolved["matched_category_group_id"],
  )

  return resolved


def main():
  print("Fetching accounts, categories, and payees from YNAB...")
  try:
    accounts_map, categories_map, payees_map, category_groups_map = get_ynab_metadata()
    print(f"Loaded {len(accounts_map)} accounts, {len(categories_map)} categories, and {len(payees_map)} payees.")
  except Exception as e:
    print(f"Failed to fetch YNAB metadata: {e}")
    exit(1)

  print("\n--- Apple Intelligence + YNAB Bot ---")
  raw_input_text = input("Enter transaction (e.g., '50 on Starbucks using ACCOUT1'): ")

  print("\nParsing, matching account, category, and payee with Apple Intelligence...")
  try:
    parsed = parse_and_match_transaction(
        raw_input_text,
        list(accounts_map.keys()),
        list(categories_map.keys()),
        list(category_groups_map.keys()),
    )
    resolved = resolve_transaction_details(
        parsed, accounts_map, categories_map, payees_map, category_groups_map
    )

    matched_account_name = resolved["matched_account_name"]
    matched_account_id = resolved["matched_account_id"]
    matched_category_name = resolved["matched_category_name"]
    matched_category_id = resolved["matched_category_id"]
    matched_payee_name = resolved["matched_payee_name"]
    matched_payee_id = resolved["matched_payee_id"]
    matched_category_group_name = resolved["matched_category_group_name"]
    matched_category_group_id = resolved["matched_category_group_id"]

    print("\n--- Extracted Details ---")
    print(f"Payee    : {matched_payee_name} (ID: {matched_payee_id or 'New Payee'})")
    print(f"Amount   : {parsed.get('amount')}")
    print(f"Date     : {parsed.get('date')}")
    print(f"Memo     : {parsed.get('memo')}")
    print(f"Account  : {matched_account_name} (ID: {matched_account_id})")
    if matched_category_id:
      category_status = matched_category_id
    elif matched_category_group_id:
      category_status = f"New in {matched_category_group_name}"
    elif matched_category_group_name:
      category_status = (
          f"New in {matched_category_group_name} -> new cat group"
      )
    else:
      category_status = "Invalid category group"
    print(f"Category : {matched_category_name} (ID: {category_status})")

    if not matched_account_id:
      print("⚠️ Warning: AI could not map a valid account from your list.")
    if not matched_category_id and (
        not matched_category_name or not matched_category_group_name
    ):
      print("⚠️ Warning: AI did not provide a category name and group.")

    confirmation = input("\nSave this transaction to YNAB? (y/n): ").strip().lower()
    if (
        confirmation == "y"
        and matched_account_id
        and matched_category_name
        and (matched_category_id or matched_category_group_name)
    ):
      push_to_ynab_via_sdk(
          parsed,
          matched_account_id,
          matched_category_id,
          matched_payee_id,
          matched_category_group_name,
          matched_category_group_id,
      )
    else:
      print("Transaction discarded or missing valid IDs.")
  except Exception as exc:
    print(f"Failed to parse or save transaction: {exc}")


if __name__ == "__main__":
  main()
