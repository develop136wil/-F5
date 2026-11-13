export function restyleNeedsReview(status) {
    return !status?.running && Number.isFinite(status?.rejected_fields) && status.rejected_fields > 0;
}
