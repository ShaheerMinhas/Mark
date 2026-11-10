/** How a stored lead relates to email lookup. Does not invent a validation result. */

export function emailPresence(lead) {
  const address = String(lead?.email || "").trim();
  if (address) return "with_email";
  const status = String(lead?.email_status || "").trim();
  const lookedUp = Boolean(lead?.email_enriched_at) || Boolean(status);
  return lookedUp ? "no_email_found" : "not_searched";
}

export function emailPresenceLabel(lead) {
  const presence = emailPresence(lead);
  if (presence === "with_email") return String(lead.email).trim();
  if (presence === "no_email_found") return "No email found";
  return "Not looked up";
}

const STORED_STATUS_LABELS = {
  verified: "Verified",
  found: "Found",
  unverified: "Unverified",
  inferred: "Inferred",
  verify_uncertain: "Uncertain",
  invalid: "Invalid",
  not_found: "No email found",
};

export function storedEmailStatusLabel(lead) {
  const status = String(lead?.email_status || "").trim();
  if (!status || emailPresence(lead) !== "with_email") return "";
  return STORED_STATUS_LABELS[status] || status;
}
