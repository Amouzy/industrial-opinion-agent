export function isFeedAutoInspecting({ canInspect, isHovered, isManuallyPaused }) {
  return Boolean(canInspect && !isHovered && !isManuallyPaused);
}

export function feedInspectionStatus({ canInspect, isHovered, isManuallyPaused }) {
  if (!canInspect) return "5 条以内不自动巡检";
  if (isManuallyPaused) return "点击暂停循环";
  if (isHovered) return "悬停暂停巡检";
  return "自动巡航中";
}
