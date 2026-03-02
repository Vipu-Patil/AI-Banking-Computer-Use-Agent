"""
Mock Legacy Banking Back-Office Application
=============================================
This is an intentionally "legacy" web application that simulates a bank's
internal member servicing system. It uses:
- Table-based layouts (no modern CSS grid/flexbox)
- No test IDs or data-testid attributes
- Non-semantic markup (font tags, inline styles, nested tables)
- Framesets to simulate legacy navigation
- Minimal JavaScript (server-rendered pages)

This is the TARGET APPLICATION that our computer-use automation system
will interact with. It is NOT part of the automation system itself.

Routes:
  /              → Frameset layout (nav + content)
  /nav           → Left navigation panel
  /home          → Welcome/home screen
  /member/search → Member search form
  /member/results?member_id=XXX → Search results
  /member/<id>/accounts → Account detail page
  /member/<id>/open-subaccount → Open sub-account form
  /member/<id>/confirm-subaccount → Confirmation page
"""

from flask import Flask, render_template, request, redirect, url_for, jsonify
import time
import random

app = Flask(__name__)

# =============================================================
# MOCK DATA — simulates a database of bank members
# =============================================================
MEMBERS = {
    "10001": {
        "id": "10001",
        "first_name": "John",
        "last_name": "Smith",
        "ssn_last4": "4532",
        "dob": "03/15/1985",
        "phone": "(555) 123-4567",
        "email": "jsmith@email.com",
        "status": "Active",
        "accounts": [
            {"type": "Savings", "number": "SAV-10001-001", "balance": "$5,230.45"},
            {"type": "Checking", "number": "CHK-10001-001", "balance": "$1,892.30"},
        ]
    },
    "10002": {
        "id": "10002",
        "first_name": "Maria",
        "last_name": "Garcia",
        "ssn_last4": "7891",
        "dob": "07/22/1990",
        "phone": "(555) 987-6543",
        "email": "mgarcia@email.com",
        "status": "Active",
        "accounts": [
            {"type": "Savings", "number": "SAV-10002-001", "balance": "$12,450.00"},
            {"type": "Checking", "number": "CHK-10002-001", "balance": "$3,201.55"},
            {"type": "CD", "number": "CD-10002-001", "balance": "$25,000.00"},
        ]
    },
    "10003": {
        "id": "10003",
        "first_name": "Robert",
        "last_name": "Johnson",
        "ssn_last4": "2345",
        "dob": "11/08/1978",
        "phone": "(555) 456-7890",
        "email": "rjohnson@email.com",
        "status": "Frozen",
        "accounts": [
            {"type": "Savings", "number": "SAV-10003-001", "balance": "$890.12"},
        ]
    },
}

# Track sub-account creation for confirmation
PENDING_ACTIONS = {}


# =============================================================
# ROUTES
# =============================================================

@app.route("/")
def index():
    """Main frameset — legacy apps often use frames for navigation."""
    return render_template("frameset.html")


@app.route("/nav")
def nav():
    """Left navigation panel inside the frameset."""
    return render_template("nav.html")


@app.route("/home")
def home():
    """Welcome screen shown in the main content frame."""
    return render_template("home.html")


@app.route("/member/search")
def member_search():
    """Member search form."""
    return render_template("search.html")


@app.route("/member/results")
def member_results():
    """Search results page. Handles found, not-found, and error cases."""
    member_id = request.args.get("member_id", "").strip()

    if not member_id:
        return render_template("search.html", error="Please enter a Member ID.")

    # Simulate slow load occasionally
    if random.random() < 0.1:
        time.sleep(2)

    # Simulate session timeout for specific test ID
    if member_id == "99999":
        return render_template("session_expired.html")

    # Simulate permission denied
    if member_id == "88888":
        return render_template("permission_denied.html")

    member = MEMBERS.get(member_id)

    if member is None:
        return render_template("not_found.html", member_id=member_id)

    return render_template("results.html", member=member)


@app.route("/member/<member_id>/accounts")
def member_accounts(member_id):
    """Account detail page for a specific member."""
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("not_found.html", member_id=member_id)

    # Check for frozen account
    if member["status"] == "Frozen":
        return render_template("account_frozen.html", member=member)

    return render_template("accounts.html", member=member)


@app.route("/member/<member_id>/open-subaccount", methods=["GET", "POST"])
def open_subaccount(member_id):
    """Form to open a new sub-account."""
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("not_found.html", member_id=member_id)

    if member["status"] == "Frozen":
        return render_template("account_frozen.html", member=member)

    if request.method == "POST":
        account_type = request.form.get("account_type", "")
        initial_deposit = request.form.get("initial_deposit", "")
        nickname = request.form.get("nickname", "")

        errors = []
        if not account_type:
            errors.append("Account type is required.")
        if not initial_deposit:
            errors.append("Initial deposit amount is required.")
        else:
            try:
                amount = float(initial_deposit.replace("$", "").replace(",", ""))
                if amount < 25.00:
                    errors.append("Minimum initial deposit is $25.00.")
                if amount > 100000.00:
                    errors.append("Maximum initial deposit is $100,000.00.")
            except ValueError:
                errors.append("Invalid deposit amount.")

        if errors:
            return render_template(
                "open_subaccount.html",
                member=member,
                errors=errors,
                account_type=account_type,
                initial_deposit=initial_deposit,
                nickname=nickname,
            )

        # Simulate processing — sometimes shows a confirmation dialog
        action_id = f"ACT-{member_id}-{int(time.time())}"
        PENDING_ACTIONS[action_id] = {
            "member_id": member_id,
            "account_type": account_type,
            "initial_deposit": initial_deposit,
            "nickname": nickname,
        }

        return redirect(url_for("confirm_subaccount", member_id=member_id, action_id=action_id))

    return render_template("open_subaccount.html", member=member)


@app.route("/member/<member_id>/confirm-subaccount")
def confirm_subaccount(member_id):
    """Confirmation page after sub-account creation."""
    action_id = request.args.get("action_id", "")
    action = PENDING_ACTIONS.get(action_id)

    if not action:
        return render_template("error.html", message="Action expired or not found. Please try again.")

    member = MEMBERS.get(member_id)
    new_account_number = f"SAV-{member_id}-{len(member['accounts']) + 1:03d}"

    return render_template(
        "confirmation.html",
        member=member,
        action=action,
        new_account_number=new_account_number,
    )


@app.route("/system/error")
def system_error():
    """Simulated system error page."""
    return render_template("error.html", message="System Error: Database connection timeout. Please try again later. (Error Code: DB-5003)")


# =============================================================
# Health check for automation to verify the app is running
# =============================================================
@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "legacy-banking-backoffice"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
