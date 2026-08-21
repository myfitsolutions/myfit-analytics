(() => {
    "use strict";
    const studioId = window.MYFIT_IMPORTS.studioId;
    const list = document.getElementById("import-history-list");
    const modal = document.getElementById("import-detail-modal");
    const content = document.getElementById("import-detail-content");
    const subtitle = document.getElementById("import-detail-subtitle");
    const rollbackButton = document.getElementById("rollback-import");
    const rollbackStatus = document.getElementById("rollback-status");
    let selected = null;
    let importType = null;
    let previewData = null;
    let presets = [];
    let activePreset = null;
    let sources = [];
    let activeSource = null;
    let editingSource = null;
    let launchingSource = false;

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        node.textContent = text;
        return node;
    }
    async function request(path, options = {}) {
        const response = await fetch(`/studios/${studioId}${path}`, options);
        if (!response.ok) {
            let message = "Request failed";
            try { const data = await response.json(); message = data.detail || message; } catch (_) {}
            throw new Error(message);
        }
        return response.json();
    }
    function formatDate(value) {
        return value ? new Date(value).toLocaleString() : "—";
    }
    function currentMapping() {
        const mapping = {};
        document.querySelectorAll("select[data-source-column]").forEach(select => {
            mapping[select.dataset.sourceColumn] = select.value || null;
        });
        return mapping;
    }
    function renderMapping(data) {
        const area = document.getElementById("mapping-area");
        area.replaceChildren(element("h3", "", "Map Your Columns"));
        const table = document.createElement("table");
        table.className = "mapping-table";
        const head = document.createElement("thead");
        const headRow = document.createElement("tr");
        headRow.append(element("th", "", "CSV Column"), element("th", "", "MyFit Field"));
        head.appendChild(headRow);
        const body = document.createElement("tbody");
        const fields = [...data.required, ...data.optional];
        data.columns.forEach(column => {
            const row = document.createElement("tr");
            const select = document.createElement("select");
            select.dataset.sourceColumn = column;
            const none = document.createElement("option");
            none.value = "";
            none.textContent = "Do not import";
            select.appendChild(none);
            fields.forEach(field => {
                const option = document.createElement("option");
                option.value = field;
                option.textContent = field.replaceAll("_", " ");
                option.selected = data.suggested_mapping[column] === field;
                select.appendChild(option);
            });
            row.append(element("td", "", column), document.createElement("td"));
            row.lastChild.appendChild(select);
            body.appendChild(row);
        });
        table.append(head, body);
        area.appendChild(table);
        document.getElementById("validate-import").hidden = false;
        document.getElementById("save-preset").hidden = false;
        document.getElementById("preset-picker").hidden = false;
        refreshPresetPicker();
    }
    function refreshPresetPicker() {
        const select = document.getElementById("saved-preset-select");
        const current = activePreset ? String(activePreset.id) : "";
        select.replaceChildren();
        const automatic = document.createElement("option");
        automatic.value = "";
        automatic.textContent = "Start with automatic mapping";
        select.appendChild(automatic);
        presets.filter(preset => preset.import_type === importType).forEach(preset => {
            const option = document.createElement("option");
            option.value = String(preset.id);
            option.textContent = preset.name;
            select.appendChild(option);
        });
        select.value = current;
    }
    function applyPreset(preset) {
        if (!previewData) return;
        const missing = Object.keys(preset.mapping).filter(source => !previewData.columns.includes(source));
        document.querySelectorAll("select[data-source-column]").forEach(select => { select.value = ""; });
        Object.entries(preset.mapping).forEach(([source, destination]) => {
            const select = [...document.querySelectorAll("select[data-source-column]")].find(item => item.dataset.sourceColumn === source);
            if (select && [...select.options].some(option => option.value === destination)) select.value = destination;
        });
        activePreset = preset;
        document.getElementById("update-preset").hidden = false;
        document.getElementById("saved-preset-select").value = String(preset.id);
        const status = document.getElementById("mapping-status");
        status.textContent = missing.length
            ? `Saved mapping expects column '${missing[0]}', but it is not present in this CSV. Fix the mapping before validation.`
            : `Applied saved mapping: ${preset.name}`;
        status.className = missing.length ? "settings-status settings-status-error" : "settings-status settings-status-success";
    }
    async function loadPresets() {
        try {
            const data = await request("/import-presets");
            presets = data.presets;
            renderPresets();
            if (importType) refreshPresetPicker();
        } catch (_) {
            document.getElementById("preset-list").replaceChildren(element("p", "crm-empty error-state", "Unable to load saved presets."));
        }
    }
    function renderPresets() {
        const container = document.getElementById("preset-list");
        container.replaceChildren();
        if (!presets.length) {
            container.appendChild(element("p", "crm-empty", "No saved mappings yet."));
            return;
        }
        presets.forEach(preset => {
            const row = document.createElement("article");
            row.className = "preset-row";
            const identity = document.createElement("div");
            identity.append(element("strong", "", preset.name), element("p", "", preset.import_type));
            const updated = element("p", "", `Updated ${formatDate(preset.updated_at)}`);
            const used = element("p", "", preset.last_used_at ? `Used ${formatDate(preset.last_used_at)}` : "Not used yet");
            const actions = document.createElement("div");
            actions.className = "preset-row-actions";
            [["Use", () => { importType = preset.import_type; activePreset = preset; document.querySelector(`[data-import-type='${preset.import_type}']`).click(); }], ["Rename", async () => {
                const name = window.prompt("Rename preset", preset.name);
                if (!name || name.trim() === preset.name) return;
                await request(`/import-presets/${preset.id}`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name: name.trim()})});
                await loadPresets();
            }], ["Delete", async () => {
                if (!window.confirm(`Delete '${preset.name}'? This removes only the saved mapping and does not affect imported records or Import History.`)) return;
                await request(`/import-presets/${preset.id}`, {method: "DELETE"});
                if (activePreset && activePreset.id === preset.id) activePreset = null;
                await loadPresets();
            }]].forEach(([label, handler]) => {
                const button = element("button", "settings-button", label); button.type = "button"; button.addEventListener("click", () => Promise.resolve(handler()).catch(() => {})); actions.appendChild(button);
            });
            row.append(identity, updated, used, actions); container.appendChild(row);
        });
    }
    function sourcePreset(source, type) { return source ? source[`${type}_preset`] : null; }
    async function loadSources() {
        try { const data = await request("/import-sources"); sources = data.sources; renderSources(); }
        catch (_) { document.getElementById("source-list").replaceChildren(element("p", "crm-empty error-state", "Unable to load data sources.")); }
    }
    function renderSources() {
        const container = document.getElementById("source-list"); container.replaceChildren();
        if (!sources.length) { container.appendChild(element("p", "crm-empty", "Save your recurring CSV setup so future imports are faster.")); return; }
        sources.forEach(source => {
            const row=document.createElement("article"); row.className="source-row"; const identity=document.createElement("div"); identity.append(element("strong","",source.name),element("p","",source.description||"CSV Data Source"));
            const configured=["members","bookings","payments"].filter(type=>sourcePreset(source,type)).length; const actions=document.createElement("div"); actions.className="preset-row-actions";
            const start=element("button","settings-button","Import Data"); start.type="button"; start.addEventListener("click",()=>{const choice=(window.prompt(`Import from ${source.name}\nChoose members, bookings, or payments`,"members")||"").trim().toLowerCase();if(!["members","bookings","payments"].includes(choice))return;activeSource=source;const assigned=sourcePreset(source,choice);activePreset=assigned?presets.find(p=>p.id===assigned.id)||null:null;launchingSource=true;document.querySelector(`[data-import-type='${choice}']`).click();launchingSource=false;document.getElementById("mapping-status").textContent=`Import from ${source.name}. Upload the ${choice} CSV.`;});
            const manage=element("button","settings-button","Manage");manage.type="button";manage.addEventListener("click",()=>openSourceModal(source));actions.append(start,manage);row.append(identity,element("p","",`${configured} of 3 mappings configured`),element("p","",source.last_used_at?`Last used: ${formatDate(source.last_used_at)}`:"Not used yet"),actions);container.appendChild(row);
        });
    }
    function fillSourceSelect(id,type,value){const select=document.getElementById(id);select.replaceChildren();const none=document.createElement("option");none.value="";none.textContent="No preset assigned";select.appendChild(none);presets.filter(p=>p.import_type===type).forEach(p=>{const option=document.createElement("option");option.value=String(p.id);option.textContent=p.name;select.appendChild(option);});select.value=value?String(value):"";}
    function openSourceModal(source=null){editingSource=source;document.getElementById("source-modal").hidden=false;document.body.classList.add("modal-open");document.getElementById("source-modal-title").textContent=source?"Manage Data Source":"Add Data Source";document.getElementById("source-name").value=source?.name||"";document.getElementById("source-description").value=source?.description||"";fillSourceSelect("source-members-preset","members",source?.members_preset?.id);fillSourceSelect("source-bookings-preset","bookings",source?.bookings_preset?.id);fillSourceSelect("source-payments-preset","payments",source?.payments_preset?.id);document.getElementById("delete-source").hidden=!source;document.getElementById("source-status").textContent="";}
    function closeSourceModal(){document.getElementById("source-modal").hidden=true;document.body.classList.remove("modal-open");}
    document.getElementById("add-source").addEventListener("click",()=>openSourceModal());
    document.getElementById("save-source").addEventListener("click",async()=>{const name=document.getElementById("source-name").value.trim(),description=document.getElementById("source-description").value.trim()||null,status=document.getElementById("source-status");try{let source=editingSource;if(source)source=await request(`/import-sources/${source.id}`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,description})});else source=await request("/import-sources",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,description})});const value=id=>{const raw=document.getElementById(id).value;return raw?Number(raw):null;};await request(`/import-sources/${source.id}/presets`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({members_preset_id:value("source-members-preset"),bookings_preset_id:value("source-bookings-preset"),payments_preset_id:value("source-payments-preset")})});await loadSources();closeSourceModal();}catch(error){status.textContent=error.message;status.className="settings-status settings-status-error";}});
    document.getElementById("delete-source").addEventListener("click",async()=>{if(!editingSource||!window.confirm(`Delete '${editingSource.name}'? Presets, imports, data, and Import History will remain.`))return;await request(`/import-sources/${editingSource.id}`,{method:"DELETE"});activeSource=null;await loadSources();closeSourceModal();});
    document.getElementById("source-modal-x").addEventListener("click",closeSourceModal);document.getElementById("close-source").addEventListener("click",closeSourceModal);
    function importFormData(includeMapping = false) {
        const file = document.getElementById("mapping-file").files[0];
        if (!file) throw new Error("No CSV selected");
        const data = new FormData();
        data.append("file", file);
        data.append("import_type", importType);
        if (includeMapping) data.append("mapping", JSON.stringify(currentMapping()));
        return data;
    }
    function renderValidation(data, heading) {
        const result = document.getElementById("mapping-result");
        result.replaceChildren(element("h3", "", heading));
        const summary = document.createElement("div");
        summary.className = "member-import-summary";
        [["Total Rows", data.total_rows], ["New", data.imported], ["Already Existing", data.skipped_existing], ["Invalid", data.invalid]].forEach(([label, value]) => {
            const item = document.createElement("div");
            item.append(element("span", "", label), element("strong", "", String(value)));
            summary.appendChild(item);
        });
        result.appendChild(summary);
        (data.errors || []).forEach(error => {
            const row = document.createElement("div");
            row.className = "member-import-error";
            row.append(element("strong", "", `Row ${error.row}`), element("span", "", error.email || "No email"), element("span", "", error.reason));
            result.appendChild(row);
        });
    }
    document.querySelectorAll("[data-import-type]").forEach(button => {
        button.addEventListener("click", () => {
            if (!launchingSource) activeSource = null;
            importType = button.dataset.importType;
            previewData = null;
            if (!activePreset || activePreset.import_type !== importType) activePreset = null;
            document.getElementById("mapping-workflow").hidden = false;
            document.getElementById("mapping-title").textContent = `Import ${importType}`;
            document.getElementById("mapping-area").replaceChildren();
            document.getElementById("mapping-result").replaceChildren();
            document.getElementById("mapping-status").textContent = "";
            document.getElementById("validate-import").hidden = true;
            document.getElementById("execute-import").hidden = true;
            document.getElementById("save-preset").hidden = true;
            document.getElementById("update-preset").hidden = true;
            document.getElementById("mapping-file").focus();
        });
    });
    document.getElementById("use-preset").addEventListener("click", () => {
        const id = Number(document.getElementById("saved-preset-select").value);
        const preset = presets.find(item => item.id === id);
        if (preset) applyPreset(preset);
    });
    document.getElementById("save-preset").addEventListener("click", async () => {
        const name = window.prompt("Save Mapping Preset\n\nName:");
        if (!name) return;
        try {
            const preset = await request("/import-presets", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name: name.trim(), import_type: importType, mapping: currentMapping()})});
            await loadPresets(); activePreset = preset; refreshPresetPicker(); document.getElementById("update-preset").hidden = false;
        } catch (error) { document.getElementById("mapping-status").textContent = error.message; }
    });
    document.getElementById("update-preset").addEventListener("click", async () => {
        if (!activePreset || !window.confirm(`Update '${activePreset.name}' with the current mapping?`)) return;
        try {
            activePreset = await request(`/import-presets/${activePreset.id}`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({mapping: currentMapping()})});
            await loadPresets();
        } catch (error) { document.getElementById("mapping-status").textContent = error.message; }
    });
    document.getElementById("preview-import").addEventListener("click", async () => {
        const status = document.getElementById("mapping-status");
        try {
            status.textContent = "Reading CSV columns...";
            previewData = await request("/imports/preview", {method: "POST", body: importFormData()});
            renderMapping(previewData);
            status.textContent = `${previewData.columns.length} columns detected. Review the suggested mappings.`;
            status.className = "settings-status settings-status-success";
        } catch (error) {
            status.textContent = error.message;
            status.className = "settings-status settings-status-error";
        }
    });
    document.getElementById("validate-import").addEventListener("click", async () => {
        const status = document.getElementById("mapping-status");
        try {
            status.textContent = "Validating without saving...";
            const data = await request("/imports/validate", {method: "POST", body: importFormData(true)});
            renderValidation(data, "Ready to Import");
            document.getElementById("execute-import").hidden = data.imported === 0;
            status.textContent = data.imported ? "Validation complete. Review the counts before confirming." : "No valid new rows to import.";
            status.className = "settings-status settings-status-success";
        } catch (error) {
            status.textContent = error.message;
            status.className = "settings-status settings-status-error";
        }
    });
    document.getElementById("execute-import").addEventListener("click", async () => {
        const status = document.getElementById("mapping-status");
        if (!window.confirm(`Confirm ${importType} import? The server will validate the CSV again.`)) return;
        try {
            status.textContent = "Importing...";
            const formData = importFormData(true);
            if (activePreset) formData.append("preset_id", String(activePreset.id));
            if (activeSource) formData.append("source_profile_id", String(activeSource.id));
            const data = await request("/imports/execute", {method: "POST", body: formData});
            renderValidation(data, "Import Complete");
            status.textContent = "Import complete.";
            status.className = "settings-status settings-status-success";
            document.getElementById("execute-import").hidden = true;
            await loadHistory();
            await loadSources();
        } catch (error) {
            status.textContent = error.message;
            status.className = "settings-status settings-status-error";
        }
    });
    async function loadHistory() {
        list.replaceChildren(element("p", "crm-empty", "Loading import history..."));
        try {
            const data = await request("/imports?limit=50");
            list.replaceChildren();
            if (!data.imports.length) {
                list.appendChild(element("p", "crm-empty", "No CSV imports have been recorded yet."));
                return;
            }
            data.imports.forEach(item => {
                const row = document.createElement("article");
                row.className = "import-history-row";
                const identity = document.createElement("div");
                identity.append(element("strong", "", item.filename), element("p", "", `${item.import_type} · ${formatDate(item.created_at)}`));
                const counts = element("p", "", `Imported: ${item.imported_count} · Skipped: ${item.skipped_count} · Invalid: ${item.invalid_count}`);
                const audit = element("p", "", `${item.status.replaceAll("_", " ")} · By: ${item.performed_by}`);
                if (item.source_name) audit.textContent += ` · Source: ${item.source_name}`;
                const view = element("button", "settings-button", "View");
                view.type = "button";
                view.addEventListener("click", () => openDetail(item.id));
                row.append(identity, counts, audit, view);
                list.appendChild(row);
            });
        } catch (error) {
            list.replaceChildren(element("p", "crm-empty error-state", "Unable to load import history."));
        }
    }
    function detailLine(label, value) {
        const row = document.createElement("div");
        row.className = "import-detail-line";
        row.append(element("span", "", label), element("strong", "", String(value)));
        return row;
    }
    async function openDetail(id) {
        modal.hidden = false;
        document.body.classList.add("modal-open");
        content.replaceChildren(element("p", "crm-empty", "Loading details..."));
        rollbackButton.hidden = true;
        rollbackStatus.textContent = "";
        try {
            selected = await request(`/imports/${id}`);
            subtitle.textContent = selected.filename;
            content.replaceChildren(
                detailLine("Type", selected.import_type),
                detailLine("Imported by", selected.performed_by),
                detailLine("Date", formatDate(selected.created_at)),
                detailLine("Status", selected.status.replaceAll("_", " ")),
                detailLine("Total rows", selected.total_rows),
                detailLine("Imported originally", selected.imported_count),
                detailLine("Already existing", selected.skipped_count),
                detailLine("Invalid", selected.invalid_count),
                detailLine("Records remaining", selected.records_remaining),
                detailLine("Protected members", selected.protected_count)
            );
            if (selected.rolled_back_at) content.appendChild(detailLine("Rolled back at", formatDate(selected.rolled_back_at)));
            if (selected.rolled_back_by) content.appendChild(detailLine("Rolled back by", selected.rolled_back_by));
            rollbackButton.hidden = !selected.rollback_eligible;
        } catch (error) {
            content.replaceChildren(element("p", "crm-empty error-state", "Unable to load import details."));
        }
    }
    function closeModal() {
        if (rollbackButton.disabled) return;
        modal.hidden = true;
        document.body.classList.remove("modal-open");
    }
    rollbackButton.addEventListener("click", async () => {
        if (!selected) return;
        const effects = selected.import_type === "payments"
            ? "This may change revenue, Payment Recovery, Action Center, and payment history."
            : selected.import_type === "bookings"
                ? "This may change retention and Action Center."
                : "Members with dependent activity will be protected and kept.";
        if (!window.confirm(`Rollback ${selected.import_type} import?\n\nThis permanently removes only records created by this import. ${effects}`)) return;
        rollbackButton.disabled = true;
        rollbackStatus.textContent = "Rolling back import...";
        try {
            const result = await request(`/imports/${selected.id}/rollback`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({confirm: true})
            });
            rollbackStatus.textContent = `${result.deleted} removed. ${result.protected} protected.`;
            rollbackStatus.className = "settings-status settings-status-success";
            await loadHistory();
            await openDetail(selected.id);
        } catch (error) {
            rollbackStatus.textContent = error.message || "Rollback failed.";
            rollbackStatus.className = "settings-status settings-status-error";
        } finally {
            rollbackButton.disabled = false;
        }
    });
    document.getElementById("import-detail-x").addEventListener("click", closeModal);
    document.getElementById("close-import-detail").addEventListener("click", closeModal);
    modal.addEventListener("click", event => { if (event.target === modal) closeModal(); });
    document.addEventListener("keydown", event => { if (event.key === "Escape" && !modal.hidden) closeModal(); });
    loadHistory();
    loadPresets();
    loadSources();
})();
