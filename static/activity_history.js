(() => {
    "use strict";

    function readableName(value) {
        const text = String(value || "Activity").replaceAll("_", " ").trim();
        return text ? text[0].toUpperCase() + text.slice(1) : "Activity";
    }

    function ordinal(value) {
        const number = Number(value);
        const lastTwo = Math.abs(number) % 100;
        const suffix = lastTwo >= 11 && lastTwo <= 13 ? "th" : ({1: "st", 2: "nd", 3: "rd"}[Math.abs(number) % 10] || "th");
        return `${number}${suffix}`;
    }

    function present(event, options = {}) {
        const labels = {
            ai_message_generated: "AI message generated", fallback_message_generated: "Standard message generated",
            message_copied: "Message copied", email_sent: "Email sent", email_failed: "Email failed",
            action_contacted: "Action marked contacted", action_resolved: "Action resolved",
            action_snoozed: "Action snoozed", action_reopened: "Action reopened",
            follow_up_scheduled: "Follow-up scheduled", follow_up_completed: "Follow-up completed",
            follow_up_cancelled: "Follow-up cancelled", attendance_milestone_celebrated: "Attendance milestone celebrated",
            attendance_milestone_dismissed: "Attendance milestone dismissed"
        };
        const typeMap = {attendance_milestone: "Milestone", attendance_decline: "Attendance", retention: "Retention", payment: "Payment", follow_up: "Follow-Up"};
        const messageNames = {attendance_milestone: "milestone", attendance_decline: "attendance check-in", retention: "retention", payment: "payment recovery"};
        const isFollowUp = String(event.event_type).startsWith("follow_up_");
        const type = isFollowUp ? "Follow-Up" : (typeMap[event.action_type] || readableName(event.action_type));
        const category = isFollowUp ? "follow_up" : ({attendance_milestone: "milestone", attendance_decline: "attendance"}[event.action_type] || event.action_type);
        let title = labels[event.event_type] || readableName(event.event_type);
        const messageName = messageNames[event.action_type] || "member";
        if (event.event_type === "email_sent") title = event.action_type === "payment" ? "Payment recovery email sent" : event.action_type === "retention" ? "Retention email sent" : "Email sent";
        if (event.event_type === "email_failed") title = event.action_type === "payment" ? "Payment recovery email failed" : event.action_type === "retention" ? "Retention email failed" : "Email failed";
        if (options.contextualTitles && event.event_type === "ai_message_generated") title = `AI ${messageName} message generated`;
        if (options.contextualTitles && event.event_type === "fallback_message_generated") title = `Standard ${messageName} message generated`;
        if (options.contextualTitles && event.event_type === "message_copied") title = `${readableName(messageName)} message copied`;
        let description = "";
        if (["ai_message_generated", "fallback_message_generated"].includes(event.event_type)) description = `${readableName(messageName)} message prepared`;
        else if (event.event_type === "message_copied") description = `${readableName(messageName)} message copied to clipboard`;
        else if (event.event_type === "email_sent") description = "Email sent to member";
        else if (event.event_type === "email_failed") description = "Email delivery failed";
        else if (isFollowUp) description = `${typeMap[event.action_type] || readableName(event.action_type)} follow-up ${event.event_type.replace("follow_up_", "")}`;
        else if (["attendance_milestone_celebrated", "attendance_milestone_dismissed"].includes(event.event_type)) {
            const milestone = event.milestone_value === 1 ? "First Class" : event.milestone_value ? `${ordinal(event.milestone_value)} Class` : "Attendance";
            description = `${milestone} milestone ${event.event_type.endsWith("celebrated") ? "celebrated" : "dismissed"}`;
        }
        const icon = isFollowUp ? "✓" : event.action_type === "attendance_milestone" ? (event.event_type === "ai_message_generated" ? "✨" : "🎉") : event.action_type === "payment" ? "💳" : event.action_type === "retention" ? "♥" : event.action_type === "attendance_decline" ? "↘" : "•";
        return {title, description, type, category, icon};
    }

    function formatDate(value, timeZone) {
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return {date: "Date unavailable", time: ""};
        const zone = timeZone ? {timeZone} : {};
        return {
            date: date.toLocaleDateString(undefined, {year: "numeric", month: "short", day: "numeric", ...zone}),
            time: date.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit", ...zone})
        };
    }

    window.MyFitActivity = {present, formatDate, readableName};
})();
