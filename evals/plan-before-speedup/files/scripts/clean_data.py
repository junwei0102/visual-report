"""Clean the raw users export: normalise emails, drop test accounts, dedupe."""
import re
import sys
import pandas as pd

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def clean_row(row):
    email = str(row["email"]).strip().lower()
    if not EMAIL_RE.match(email):
        email = ""
    name = " ".join(str(row["name"]).split())
    is_test = name.lower().startswith("test") or email.endswith("@example.com")
    return pd.Series({"id": row["id"], "email": email, "name": name, "is_test": is_test})


def main(src, dst):
    df = pd.read_csv(src)
    rows = []
    for _, row in df.iterrows():
        rows.append(clean_row(row))
    out = pd.DataFrame(rows)
    out = out[~out["is_test"]].drop(columns=["is_test"])
    out = out.drop_duplicates(subset=["email"], keep="first")
    out.to_csv(dst, index=False)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
