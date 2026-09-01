(() => {
    "use strict";

    const config = window.MYFIT_CRM;
    const page = document.body.dataset.crmPage;
    let crmData = null;
    let activeMessageType = null;

    function studioUrl(path) {
        return `/studios/${config.studioId}${path}`;
    }

    function formatCurrency(value, currency) {
        return new Intl.NumberFormat(undefined, {
            style: "currency",
            currency: currency,
            maximumFractionDigits: 2
        }).format(Number(value) || 0);
    }

    function formatDate(value, withTime = false) {
        if (!value) return "No attended visits yet";
        const options = withTime
            ? {year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}
            : {year: "numeric", month: "short", day: "numeric"};
        if (crmData?.studio?.timezone) options.timeZone = crmData.studio.timezone;
        return new Date(value).toLocaleString(undefined, options);
    }

    function formatActivityDate(value) {
        return window.MyFitActivity.formatDate(value, crmData?.studio?.timezone);
    }

    function textElement(tag, className, text) {
        const element = document.createElement(tag);
        if (className) element.className = className;
        element.textContent = text;
        return element;
    }

    async function request(url, options = {}) {
        const response = await fetch(url, options);
        if (!response.ok) {
            let detail = "Request failed";
            try {
                const data = await response.json();
                if (typeof data.detail === "string") detail = data.detail;
            } catch (_) {
                // Keep the controlled fallback.
            }
            throw new Error(detail);
        }
        return response.json();
    }

    function initMembersPage() {
        const list = document.getElementById("members-list");
        const search = document.getElementById("member-search");
        const count = document.getElementById("member-count");
        const filters = document.getElementById("member-filters");
        let members = [];
        let currency = "PHP";
        let activeFilter = "all";

        function render() {
            const query = search.value.trim().toLowerCase();
            const visible = members.filter(member => {
                const matchesSearch =
                    member.name.toLowerCase().includes(query)
                    || member.email.toLowerCase().includes(query);
                const matchesFilter =
                    activeFilter === "all"
                    || (activeFilter === "payment_issue"
                        ? member.failed_payment_count > 0
                        : member.retention_status === activeFilter);
                return matchesSearch && matchesFilter;
            });
            list.replaceChildren();
            count.textContent = `${visible.length} of ${members.length} members`;

            if (!visible.length) {
                if (!members.length) {
                    list.appendChild(textElement("p", "crm-empty", "No member data yet. Import members to build your member base and unlock retention analysis."));
                    if (document.getElementById("open-member-import")) {
                        const action = textElement("a", "settings-button", "Import Members");
                        action.href = "/imports#import-members";
                        list.appendChild(action);
                    }
                } else list.appendChild(textElement("p", "crm-empty", "No members match this search or filter."));
                return;
            }

            visible.forEach(member => {
                const item = document.createElement("article");
                item.className = "crm-member-row";
                const identity = document.createElement("div");
                identity.append(
                    textElement("strong", "", member.name),
                    textElement("p", "", member.email)
                );
                const health = document.createElement("div");
                const status = textElement(
                    "span",
                    `crm-status crm-status-${member.retention_status}`,
                    member.retention_status.replace("_", " ")
                );
                health.appendChild(status);
                health.appendChild(textElement(
                    "p",
                    "",
                    member.days_inactive === null
                        ? "No attended visits yet"
                        : `${member.days_inactive} days inactive`
                ));
                health.appendChild(textElement(
                    "p",
                    "",
                    member.last_visit_at
                        ? `Last visit: ${formatDate(member.last_visit_at)}`
                        : "No attended visits yet"
                ));
                if (member.failed_payment_count > 0) {
                    health.appendChild(textElement(
                        "p",
                        "crm-payment-issue",
                        `Payment Issue: ${formatCurrency(member.failed_amount, currency)}`
                    ));
                }
                const link = textElement("a", "action-button crm-view-link", "View Member");
                link.href = `/members/${member.id}`;
                item.append(identity, health, link);
                list.appendChild(item);
            });
        }

        search.addEventListener("input", render);
        filters.addEventListener("click", event => {
            const button = event.target.closest("button[data-filter]");
            if (!button) return;
            activeFilter = button.dataset.filter;
            filters.querySelectorAll("button").forEach(item => {
                item.classList.toggle("active", item === button);
            });
            render();
        });

        async function loadMembers() {
            try {
                const data = await request("/api/members");
                members = data.members;
                currency = data.currency;
                render();
            } catch (error) {
                console.error("Members loading error:", error);
                list.replaceChildren(textElement("p", "crm-empty error-state", "Unable to load members."));
                count.textContent = "Members unavailable";
            }
        }

        const importModal = document.getElementById("member-import-modal");

        if (importModal) {
            const importForm = document.getElementById("member-import-form");
            const importFile = document.getElementById("member-csv-file");
            const importButton = document.getElementById("import-members-button");
            const importStatus = document.getElementById("member-import-status");
            const importResult = document.getElementById("member-import-result");

            function closeImportModal() {
                if (importButton.disabled) return;
                importModal.hidden = true;
                document.body.classList.remove("modal-open");
            }

            function renderImportResult(data) {
                importResult.replaceChildren();
                const summary = document.createElement("div");
                summary.className = "member-import-summary";
                [["Total Rows", data.total_rows], ["Imported", data.imported], ["Already Existing", data.skipped_existing], ["Invalid", data.invalid]].forEach(([label, value]) => {
                    const item = document.createElement("div");
                    item.append(textElement("span", "", label), textElement("strong", "", String(value)));
                    summary.appendChild(item);
                });
                importResult.appendChild(summary);
                if (data.errors.length) {
                    const heading = textElement("h3", "", "Invalid Rows");
                    importResult.appendChild(heading);
                    data.errors.forEach(error => {
                        const row = document.createElement("div");
                        row.className = "member-import-error";
                        row.append(
                            textElement("strong", "", `Row ${error.row}`),
                            textElement("span", "", error.email || "No email"),
                            textElement("span", "", error.reason)
                        );
                        importResult.appendChild(row);
                    });
                    if (data.errors_truncated) {
                        importResult.appendChild(textElement("p", "crm-empty", "Additional invalid rows are not displayed."));
                    }
                }
            }

            importForm.addEventListener("submit", async event => {
                event.preventDefault();
                const file = importFile.files[0];

                if (!file || !file.name.toLowerCase().endsWith(".csv")) {
                    importStatus.textContent = "Choose a CSV file.";
                    importStatus.className = "settings-status settings-status-error";
                    return;
                }

                if (file.size > 5 * 1024 * 1024) {
                    importStatus.textContent = "File exceeds 5 MB limit.";
                    importStatus.className = "settings-status settings-status-error";
                    return;
                }

                if (!window.confirm(`Import members from ${file.name}? Existing members will not be updated.`)) return;

                importButton.disabled = true;
                importButton.textContent = "Importing...";
                importStatus.textContent = "Importing members...";
                importStatus.className = "settings-status";
                importResult.replaceChildren();
                const formData = new FormData();
                formData.append("file", file);

                try {
                    const data = await request(
                        studioUrl("/members/import"),
                        {method: "POST", body: formData}
                    );
                    importStatus.textContent = "Import complete.";
                    importStatus.className = "settings-status settings-status-success";
                    renderImportResult(data);
                    importForm.reset();
                    await loadMembers();
                } catch (error) {
                    console.error("Member import error:", error);
                    importStatus.textContent = error.message || "Unable to import members.";
                    importStatus.className = "settings-status settings-status-error";
                } finally {
                    importButton.disabled = false;
                    importButton.textContent = "Import Members";
                }
            });

            document.getElementById("open-member-import").addEventListener("click", () => {
                importModal.hidden = false;
                document.body.classList.add("modal-open");
                importStatus.textContent = "";
                importResult.replaceChildren();
                importFile.focus();
            });
            document.getElementById("member-import-x").addEventListener("click", closeImportModal);
            document.getElementById("close-member-import").addEventListener("click", closeImportModal);
            importModal.addEventListener("click", event => { if (event.target === importModal) closeImportModal(); });
            document.addEventListener("keydown", event => { if (event.key === "Escape" && !importModal.hidden) closeImportModal(); });
        }

        const bookingImportModal = document.getElementById("booking-import-modal");

        if (bookingImportModal) {
            const bookingImportForm = document.getElementById("booking-import-form");
            const bookingImportFile = document.getElementById("booking-csv-file");
            const bookingImportButton = document.getElementById("import-bookings-button");
            const bookingImportStatus = document.getElementById("booking-import-status");
            const bookingImportResult = document.getElementById("booking-import-result");

            function closeBookingImportModal() {
                if (bookingImportButton.disabled) return;
                bookingImportModal.hidden = true;
                document.body.classList.remove("modal-open");
            }

            function renderBookingImportResult(data) {
                bookingImportResult.replaceChildren();
                bookingImportResult.appendChild(textElement("h3", "", "Booking Import Complete"));
                const summary = document.createElement("div");
                summary.className = "member-import-summary";
                [["Total Rows", data.total_rows], ["Imported", data.imported], ["Already Existing", data.skipped_existing], ["Invalid", data.invalid]].forEach(([label, value]) => {
                    const item = document.createElement("div");
                    item.append(textElement("span", "", label), textElement("strong", "", String(value)));
                    summary.appendChild(item);
                });
                bookingImportResult.appendChild(summary);
                if (data.errors.length) {
                    bookingImportResult.appendChild(textElement("h3", "", "Invalid Rows"));
                    data.errors.forEach(error => {
                        const row = document.createElement("div");
                        row.className = "member-import-error";
                        row.append(
                            textElement("strong", "", `Row ${error.row}`),
                            textElement("span", "", error.email || "No email"),
                            textElement("span", "", error.reason)
                        );
                        bookingImportResult.appendChild(row);
                    });
                    if (data.errors_truncated) {
                        bookingImportResult.appendChild(textElement("p", "crm-empty", "Additional invalid rows are not displayed."));
                    }
                }
            }

            bookingImportForm.addEventListener("submit", async event => {
                event.preventDefault();
                const file = bookingImportFile.files[0];
                if (!file || !file.name.toLowerCase().endsWith(".csv")) {
                    bookingImportStatus.textContent = "Choose a CSV file.";
                    bookingImportStatus.className = "settings-status settings-status-error";
                    return;
                }
                if (file.size > 5 * 1024 * 1024) {
                    bookingImportStatus.textContent = "File exceeds 5 MB limit.";
                    bookingImportStatus.className = "settings-status settings-status-error";
                    return;
                }
                if (!window.confirm(`Import bookings from ${file.name}? Existing bookings will not be updated.`)) return;

                bookingImportButton.disabled = true;
                bookingImportButton.textContent = "Importing...";
                bookingImportStatus.textContent = "Importing bookings...";
                bookingImportStatus.className = "settings-status";
                bookingImportResult.replaceChildren();
                const formData = new FormData();
                formData.append("file", file);
                try {
                    const data = await request(
                        studioUrl("/bookings/import"),
                        {method: "POST", body: formData}
                    );
                    bookingImportStatus.textContent = "Import complete.";
                    bookingImportStatus.className = "settings-status settings-status-success";
                    renderBookingImportResult(data);
                    bookingImportForm.reset();
                    await loadMembers();
                } catch (error) {
                    console.error("Booking import error:", error);
                    bookingImportStatus.textContent = error.message || "Unable to import bookings.";
                    bookingImportStatus.className = "settings-status settings-status-error";
                } finally {
                    bookingImportButton.disabled = false;
                    bookingImportButton.textContent = "Import Bookings";
                }
            });

            document.getElementById("open-booking-import").addEventListener("click", () => {
                bookingImportModal.hidden = false;
                document.body.classList.add("modal-open");
                bookingImportStatus.textContent = "";
                bookingImportResult.replaceChildren();
                bookingImportFile.focus();
            });
            document.getElementById("booking-import-x").addEventListener("click", closeBookingImportModal);
            document.getElementById("close-booking-import").addEventListener("click", closeBookingImportModal);
            bookingImportModal.addEventListener("click", event => { if (event.target === bookingImportModal) closeBookingImportModal(); });
            document.addEventListener("keydown", event => { if (event.key === "Escape" && !bookingImportModal.hidden) closeBookingImportModal(); });
        }

        const paymentImportModal = document.getElementById("payment-import-modal");

        if (paymentImportModal) {
            const paymentImportForm = document.getElementById("payment-import-form");
            const paymentImportFile = document.getElementById("payment-csv-file");
            const paymentImportButton = document.getElementById("import-payments-button");
            const paymentImportStatus = document.getElementById("payment-import-status");
            const paymentImportResult = document.getElementById("payment-import-result");

            function closePaymentImportModal() {
                if (paymentImportButton.disabled) return;
                paymentImportModal.hidden = true;
                document.body.classList.remove("modal-open");
            }

            function renderPaymentImportResult(data) {
                paymentImportResult.replaceChildren();
                paymentImportResult.appendChild(textElement("h3", "", "Payment Import Complete"));
                const summary = document.createElement("div");
                summary.className = "member-import-summary";
                [["Total Rows", data.total_rows], ["Imported", data.imported], ["Already Existing", data.skipped_existing], ["Invalid", data.invalid]].forEach(([label, value]) => {
                    const item = document.createElement("div");
                    item.append(textElement("span", "", label), textElement("strong", "", String(value)));
                    summary.appendChild(item);
                });
                paymentImportResult.appendChild(summary);
                if (data.errors.length) {
                    paymentImportResult.appendChild(textElement("h3", "", "Invalid Rows"));
                    data.errors.forEach(error => {
                        const row = document.createElement("div");
                        row.className = "member-import-error";
                        row.append(
                            textElement("strong", "", `Row ${error.row}`),
                            textElement("span", "", error.email || "No email"),
                            textElement("span", "", error.reason)
                        );
                        paymentImportResult.appendChild(row);
                    });
                    if (data.errors_truncated) {
                        paymentImportResult.appendChild(textElement("p", "crm-empty", "Additional invalid rows are not displayed."));
                    }
                }
            }

            paymentImportForm.addEventListener("submit", async event => {
                event.preventDefault();
                const file = paymentImportFile.files[0];
                if (!file || !file.name.toLowerCase().endsWith(".csv")) {
                    paymentImportStatus.textContent = "Choose a CSV file.";
                    paymentImportStatus.className = "settings-status settings-status-error";
                    return;
                }
                if (file.size > 5 * 1024 * 1024) {
                    paymentImportStatus.textContent = "File exceeds 5 MB limit.";
                    paymentImportStatus.className = "settings-status settings-status-error";
                    return;
                }
                if (!window.confirm(`Import payments from ${file.name}? Existing payments will not be updated.`)) return;

                paymentImportButton.disabled = true;
                paymentImportButton.textContent = "Importing...";
                paymentImportStatus.textContent = "Importing payments...";
                paymentImportStatus.className = "settings-status";
                paymentImportResult.replaceChildren();
                const formData = new FormData();
                formData.append("file", file);
                try {
                    const data = await request(
                        studioUrl("/payments/import"),
                        {method: "POST", body: formData}
                    );
                    paymentImportStatus.textContent = "Import complete.";
                    paymentImportStatus.className = "settings-status settings-status-success";
                    renderPaymentImportResult(data);
                    paymentImportForm.reset();
                    await loadMembers();
                } catch (error) {
                    console.error("Payment import error:", error);
                    paymentImportStatus.textContent = error.message || "Unable to import payments.";
                    paymentImportStatus.className = "settings-status settings-status-error";
                } finally {
                    paymentImportButton.disabled = false;
                    paymentImportButton.textContent = "Import Payments";
                }
            });

            document.getElementById("open-payment-import").addEventListener("click", () => {
                paymentImportModal.hidden = false;
                document.body.classList.add("modal-open");
                paymentImportStatus.textContent = "";
                paymentImportResult.replaceChildren();
                paymentImportFile.focus();
            });
            document.getElementById("payment-import-x").addEventListener("click", closePaymentImportModal);
            document.getElementById("close-payment-import").addEventListener("click", closePaymentImportModal);
            paymentImportModal.addEventListener("click", event => { if (event.target === paymentImportModal) closePaymentImportModal(); });
            document.addEventListener("keydown", event => { if (event.key === "Escape" && !paymentImportModal.hidden) closePaymentImportModal(); });
        }

        loadMembers();
    }

    function activityPresentation(event) {
        return window.MyFitActivity.present(event);
    }

    function renderDetail() {
        const member = crmData.member;
        const retention = crmData.retention;
        document.getElementById("profile-name").textContent = member.name;
        document.getElementById("profile-email").textContent = member.email;
        const health = document.getElementById("profile-health");
        health.replaceChildren(
            textElement("span", `crm-status crm-status-${retention.status}`, retention.status.replace("_", " ")),
            textElement("span", "", retention.days_inactive === null ? "No attended visits yet" : `${retention.days_inactive} days inactive`),
            textElement("span", "", retention.last_visit_at ? `Last visit: ${formatDate(retention.last_visit_at)}` : "No attended visits yet")
        );
        renderProfileActions();
        renderSummary();
        renderAttendance();
        renderRetention();
        renderPayments();
        renderBookings();
        renderFollowUps();
        renderActivity();
    }

    function renderAttendance() {
        const data = crmData.attendance;
        const container = document.getElementById("attendance-detail");
        const trend = !data.has_enough_history ? "Not enough attendance history yet."
            : data.attendance_declining ? `Declining: ${data.baseline_visits_per_week} previous vs ${data.recent_visits_per_week} recent visits/week (↓ ${data.attendance_change_percent}%).`
            : "Attendance is stable.";
        const milestone = data.last_milestone ? `Last milestone: ${data.last_milestone} (${data.milestone_status || "open"}).` : "No attendance milestone reached yet.";
        const next = data.next_milestone ? `Next milestone: ${data.total_attended} / ${data.next_milestone} — ${data.visits_until_next_milestone} visits to go.` : "All configured milestones reached.";
        container.replaceChildren(
            textElement("p", "", `Lifetime Visits: ${data.total_attended}`),
            textElement("p", "", `Average Visits / Week: ${data.average_visits_per_week}`),
            textElement("p", "", `Last Visit: ${data.last_visit_at ? formatDate(data.last_visit_at) : "No attended visits yet"}`),
            textElement("p", "", milestone), textElement("p", "", next), textElement("p", "", trend)
        );
    }

    function renderProfileActions() {
        const actions = document.getElementById("profile-actions");
        actions.replaceChildren();
        const contact = textElement("button", "action-button", "Contact Member");
        contact.type = "button";
        contact.addEventListener("click", () => openMessage("retention", contact));
        actions.appendChild(contact);
        if (crmData.payment_summary.failed_count > 0) {
            const recover = textElement("button", "action-button", "Recover Payment");
            recover.type = "button";
            recover.addEventListener("click", () => openMessage("payment", recover));
            actions.appendChild(recover);
        }
        const schedule = document.createElement("select");
        schedule.className = "action-snooze-select";
        schedule.setAttribute("aria-label", "Schedule follow-up");
        [["", "Schedule Follow-Up"], ["1", "In 1 day"], ["3", "In 3 days"], ["7", "In 7 days"]].forEach(([value, label]) => {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = Number(value) === crmData.studio.default_follow_up_days ? `${label} (Default)` : label;
            schedule.appendChild(option);
        });
        schedule.addEventListener("change", async () => {
            const days = Number(schedule.value);
            if (!days) return;
            schedule.disabled = true;
            try {
                await createFollowUp("retention", days);
                await loadDetail();
            } catch (error) {
                showState("Unable to schedule follow-up.", true);
            } finally {
                schedule.value = "";
                schedule.disabled = false;
            }
        });
        actions.appendChild(schedule);
    }

    function renderSummary() {
        const container = document.getElementById("profile-summary");
        const booking = crmData.booking_summary;
        const payment = crmData.payment_summary;
        const values = [
            ["Total Bookings", booking.total], ["Attended", booking.attended],
            ["Cancelled", booking.cancelled], ["No Shows", booking.no_shows],
            ["Total Paid", formatCurrency(payment.total_paid, crmData.studio.currency)],
            ["Failed Payments", payment.failed_count],
            ["Failed Amount", formatCurrency(payment.failed_amount, crmData.studio.currency)]
        ];
        container.replaceChildren();
        values.forEach(([label, value]) => {
            const card = document.createElement("div");
            card.className = "card";
            card.append(textElement("p", "card-label", label), textElement("h3", "", String(value)));
            container.appendChild(card);
        });
    }

    function renderRetention() {
        const retention = document.getElementById("retention-detail");
        retention.replaceChildren(
            textElement("strong", "", crmData.retention.status.replace("_", " ")),
            textElement("p", "", crmData.retention.days_inactive === null ? "No attended visits yet" : `${crmData.retention.days_inactive} days inactive`),
            textElement("p", "", crmData.retention.last_visit_at ? `Last visit: ${formatDate(crmData.retention.last_visit_at)}` : "No attended visits yet")
        );
        const statuses = document.getElementById("action-status-detail");
        statuses.replaceChildren();
        Object.keys(crmData.action_statuses).forEach(type => {
            const row = document.createElement("div");
            row.className = "crm-action-status-row";
            row.append(
                textElement("strong", "", `${type[0].toUpperCase() + type.slice(1)}:`),
                textElement("span", "action-current-status", crmData.action_statuses[type])
            );
            if (config.userRole !== "staff") {
                const resolve = textElement("button", "action-status-button", "Resolve");
                resolve.type = "button";
                resolve.addEventListener("click", () => updateActionStatus(type, "resolved"));
                const snooze = document.createElement("select");
                snooze.className = "action-snooze-select";
                [["", "Snooze"], ["1", "1 day"], ["3", "3 days"], ["7", "7 days"]].forEach(([value, label]) => {
                    const option = document.createElement("option"); option.value = value; option.textContent = label; snooze.appendChild(option);
                });
                snooze.addEventListener("change", () => {
                    if (snooze.value) updateActionStatus(type, "snoozed", Number(snooze.value));
                });
                row.append(resolve, snooze);
                if (crmData.action_statuses[type] !== "open") {
                    const reopen = textElement("button", "action-status-button", "Reopen");
                    reopen.type = "button";
                    reopen.addEventListener("click", () => updateActionStatus(type, "open"));
                    row.appendChild(reopen);
                }
            }
            statuses.appendChild(row);
        });
    }

    function renderPayments() {
        const summary = crmData.payment_summary;
        const details = document.getElementById("payment-summary");
        details.replaceChildren(
            textElement("p", "", `Total Paid: ${formatCurrency(summary.total_paid, crmData.studio.currency)}`),
            textElement("p", "", `Failed Payments: ${summary.failed_count}`),
            textElement("p", "", `Failed Amount: ${formatCurrency(summary.failed_amount, crmData.studio.currency)}`),
            textElement("p", "", `Recovery workflow: ${summary.workflow_status === "resolved" ? "Resolved operationally" : summary.workflow_status}`)
        );
        if (summary.later_matching_payment) details.appendChild(
            textElement("p", "", "Later matching successful payment recorded; recovery is not automatically confirmed.")
        );
        const list = document.getElementById("payment-history");
        list.replaceChildren();
        if (!crmData.recent_payments.length) return list.appendChild(textElement("p", "crm-empty", "No payments recorded."));
        crmData.recent_payments.forEach(payment => {
            const row = document.createElement("div"); row.className = "crm-history-row";
            row.append(textElement("time", "", formatDate(payment.payment_date)), textElement("strong", "", formatCurrency(payment.amount, crmData.studio.currency)), textElement("span", "", payment.status.replace("_", " ")));
            list.appendChild(row);
        });
    }

    function renderBookings() {
        const list = document.getElementById("booking-history");
        list.replaceChildren();
        if (!crmData.recent_bookings.length) return list.appendChild(textElement("p", "crm-empty", "No bookings recorded."));
        crmData.recent_bookings.forEach(booking => {
            const row = document.createElement("div"); row.className = "crm-history-row";
            row.append(textElement("time", "", formatDate(booking.booking_date)), textElement("strong", "", booking.class_name), textElement("span", "", booking.status.replace("_", " ")));
            list.appendChild(row);
        });
    }

    function renderFollowUps() {
        const list = document.getElementById("member-follow-ups");
        list.replaceChildren();
        if (!crmData.follow_ups.length) return list.appendChild(textElement("p", "crm-empty", "No follow-ups recorded."));
        const now = Date.now();
        crmData.follow_ups.forEach(followUp => {
            const row = document.createElement("div"); row.className = "crm-follow-up-row";
            const state = followUp.status === "pending" ? (new Date(followUp.due_at).getTime() <= now ? "DUE NOW" : "UPCOMING") : followUp.status.toUpperCase();
            const content = document.createElement("div");
            content.append(textElement("strong", "", `${state} · ${followUp.action_type.toUpperCase()}`), textElement("p", "", formatDate(followUp.due_at, true)));
            if (followUp.note) content.appendChild(textElement("p", "", followUp.note));
            const controls = document.createElement("div"); controls.className = "follow-up-controls";
            if (followUp.status === "pending") {
                const operation = new Date(followUp.due_at).getTime() <= now ? "complete" : "cancel";
                const button = textElement("button", "action-status-button", operation === "complete" ? "Complete" : "Cancel");
                button.type = "button"; button.addEventListener("click", () => updateFollowUp(followUp.id, operation)); controls.appendChild(button);
                if (operation === "complete") {
                    const contact = textElement("button", "action-button", followUp.action_type === "payment" ? "Recover Payment" : "Contact Member");
                    contact.type = "button"; contact.addEventListener("click", () => openMessage(followUp.action_type, contact)); controls.prepend(contact);
                }
            }
            row.append(content, controls); list.appendChild(row);
        });
    }

    function renderActivity() {
        if (!crmData) return;
        const list = document.getElementById("member-activity");
        const filter = document.getElementById("activity-filter");
        list.replaceChildren();
        if (!crmData.action_history.length) return list.appendChild(textElement("p", "crm-empty", "No activity recorded."));
        const selected = filter.value;
        crmData.action_history.forEach(event => {
            const presentation = activityPresentation(event);
            if (selected !== "all" && presentation.category !== selected) return;
            const row = document.createElement("article"); row.className = "crm-activity-row";
            const timestamp = formatActivityDate(event.created_at);
            const time = document.createElement("time"); time.dateTime = event.created_at;
            time.append(textElement("span", "crm-activity-date", timestamp.date), textElement("span", "crm-activity-time", timestamp.time));
            const content = document.createElement("div"); content.className = "crm-activity-content";
            content.append(textElement("strong", "crm-activity-title", `${presentation.icon} ${presentation.title}`));
            if (presentation.description) content.appendChild(textElement("span", "crm-activity-description", presentation.description));
            const badge = textElement("span", `crm-activity-badge crm-activity-badge-${presentation.category}`, presentation.type);
            row.append(time, content, badge);
            list.appendChild(row);
        });
        if (!list.children.length) list.appendChild(textElement("p", "crm-empty", "No activity matches this filter."));
    }

    function showState(message, isError = false) {
        const state = document.getElementById("profile-state");
        state.textContent = message;
        state.className = `crm-page-state${isError ? " error-state" : ""}`;
    }

    async function loadDetail() {
        crmData = await request(`/api/members/${config.memberId}`);
        renderDetail();
        showState("");
    }

    async function createFollowUp(actionType, days) {
        await request(studioUrl("/follow-ups"), {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({member_id: config.memberId, action_type: actionType, due_at: new Date(Date.now() + days * 86400000).toISOString(), note: null})
        });
        showState("Follow-up scheduled.");
    }

    async function updateFollowUp(id, operation) {
        try {
            await request(studioUrl(`/follow-ups/${id}/${operation}`), {method: "POST"});
            await loadDetail();
        } catch (error) { showState("Unable to update follow-up.", true); }
    }

    async function updateActionStatus(actionType, status, days = null) {
        const snoozeUntil = days ? new Date(Date.now() + days * 86400000).toISOString() : null;
        try {
            await request(studioUrl("/action-status"), {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({member_id: config.memberId, action_type: actionType, status, snooze_until: snoozeUntil})
            });
            await loadDetail();
        } catch (error) { showState("Unable to update action status.", true); }
    }

    function fallbackMessage(type) {
        const firstName = crmData.member.name.split(/\s+/)[0];
        if (type === "payment") {
            return `Hi ${firstName},\n\nIt looks like there was an issue processing your recent payment of ${formatCurrency(crmData.payment_summary.failed_amount, crmData.studio.currency)}.\n\nPlease update your payment details when convenient. Let us know if you need any help.`;
        }
        return `Hi ${firstName},\n\nWe noticed it's been a little while since your last visit. We'd love to see you back at the studio.\n\nLet us know if there's anything we can help with.`;
    }

    async function logMessageEvent(eventType, message) {
        try {
            await request(studioUrl("/action-history"), {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({member_id: config.memberId, action_type: activeMessageType, event_type: eventType, message_text: message})
            });
        } catch (error) { console.error("Activity logging error:", error); }
    }

    async function openMessage(type, trigger) {
        activeMessageType = type;
        const modal = document.getElementById("crm-message-modal");
        const message = document.getElementById("crm-message");
        document.getElementById("crm-message-member").textContent = crmData.member.name;
        document.getElementById("crm-subject").value = type === "payment" ? "Payment update needed" : "We'd love to see you back";
        document.getElementById("crm-send").hidden = config.userRole === "staff";
        document.getElementById("crm-message-status").textContent = "";
        message.value = "Generating suggested message...";
        modal.hidden = false;
        document.body.classList.add("modal-open");
        trigger.dataset.modalTrigger = "true";
        try {
            const data = await request(studioUrl("/action-message"), {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({member_id: config.memberId, member_name: crmData.member.name, action_type: type, retention_status: crmData.retention.status, days_inactive: crmData.retention.days_inactive, failed_amount: crmData.payment_summary.failed_amount})
            });
            message.value = data.message;
        } catch (error) {
            message.value = fallbackMessage(type);
            document.getElementById("crm-message-notice").textContent = "Using a standard message; you can edit it before copying.";
            logMessageEvent("fallback_message_generated", message.value);
        }
    }

    function closeMessage() {
        document.getElementById("crm-message-modal").hidden = true;
        document.body.classList.remove("modal-open");
    }

    function initMessageModal() {
        const modal = document.getElementById("crm-message-modal");
        document.getElementById("crm-message-x").addEventListener("click", closeMessage);
        document.getElementById("crm-close").addEventListener("click", closeMessage);
        modal.addEventListener("click", event => { if (event.target === modal) closeMessage(); });
        document.addEventListener("keydown", event => { if (event.key === "Escape" && !modal.hidden) closeMessage(); });
        document.getElementById("crm-copy").addEventListener("click", async () => {
            const message = document.getElementById("crm-message");
            try {
                if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(message.value);
                else { message.select(); document.execCommand("copy"); }
                document.getElementById("crm-message-status").textContent = "Copied!";
                await logMessageEvent("message_copied", message.value);
            } catch (error) { document.getElementById("crm-message-status").textContent = "Copy failed. Copy the selected message manually."; }
        });
        document.getElementById("crm-send").addEventListener("click", async event => {
            const button = event.currentTarget;
            button.disabled = true;
            button.textContent = "Sending...";
            try {
                await request(studioUrl("/send-action-email"), {
                    method: "POST", headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({member_id: config.memberId, action_type: activeMessageType, subject: document.getElementById("crm-subject").value, message: document.getElementById("crm-message").value})
                });
                document.getElementById("crm-message-status").textContent = "Email sent successfully.";
                await loadDetail();
            } catch (error) { document.getElementById("crm-message-status").textContent = "Email could not be sent."; }
            finally { button.disabled = false; button.textContent = "Send Email"; }
        });
    }

    if (page === "members") initMembersPage();
    if (page === "detail") {
        initMessageModal();
        document.getElementById("activity-filter").addEventListener("change", renderActivity);
        loadDetail().catch(error => {
            console.error("Member detail error:", error);
            showState("Unable to load this member.", true);
        });
    }
})();
