(() => {
    "use strict";
    const storageKey="myfit-analytics-theme";
    const preferred=()=>"dark";
    const stored=localStorage.getItem(storageKey);
    document.documentElement.dataset.theme=stored==="light"||stored==="dark"?stored:preferred();

    const icon=(name)=>({dashboard:"⌂",reports:"▤",revenue:"↗",members:"◉",imports:"⇧",settings:"⚙"}[name]||"•");
    const pageTitle=()=>document.body.dataset.pageTitle||({"/dashboard":"Dashboard","/reports":"Reports","/revenue":"Revenue Intelligence","/members":"Members","/imports":"Data Imports","/onboarding":"Studio Setup"}[location.pathname]||(location.pathname.startsWith("/members/")?"Member Intelligence":"MyFit Analytics"));
    const currentNavigationHref=()=>location.pathname.startsWith("/members/")?"/members":location.pathname;
    const navItem=(label,href,key)=>{const link=document.createElement("a");link.className="app-nav-link";link.href=href;link.innerHTML=`<span aria-hidden="true">${icon(key)}</span><span>${label}</span>`;if(currentNavigationHref()===href)link.setAttribute("aria-current","page");return link;};
    const group=(label,items)=>{const section=document.createElement("section");section.className="app-nav-group";const title=document.createElement("p");title.textContent=label;section.append(title,...items);return section;};
    const themeButton=()=>{const button=document.createElement("button");button.type="button";button.className="theme-toggle";const render=()=>{const dark=document.documentElement.dataset.theme==="dark";button.innerHTML=`<span aria-hidden="true">${dark?"☀":"☾"}</span><span>${dark?"Light mode":"Dark mode"}</span>`;button.setAttribute("aria-label",dark?"Switch to light mode":"Switch to dark mode");};button.addEventListener("click",()=>{const next=document.documentElement.dataset.theme==="dark"?"light":"dark";document.documentElement.dataset.theme=next;localStorage.setItem(storageKey,next);render();window.dispatchEvent(new CustomEvent("myfit-theme-change",{detail:{theme:next}}));});render();return button;};

    document.addEventListener("DOMContentLoaded",()=>{
        const header=document.querySelector(".dashboard-header"),main=document.querySelector(".dashboard");
        if(!header||!main||location.pathname==="/onboarding"){document.body.append(themeButton());return;}
        document.body.classList.add("app-shell-enabled");main.dataset.pageTitle=pageTitle();
        const heading=header.querySelector("h1");if(heading)heading.textContent=pageTitle();
        const importsAvailable=Boolean(header.querySelector('a[href="/imports"]'));
        const sidebar=document.createElement("aside");sidebar.className="app-sidebar";sidebar.id="app-sidebar";sidebar.setAttribute("aria-label","Application navigation");
        const brand=document.createElement("a");brand.className="app-brand";brand.href="/dashboard";brand.innerHTML='<span class="app-brand-mark" aria-hidden="true">M</span><span><strong>MyFit</strong><small>ANALYTICS</small></span>';
        const nav=document.createElement("nav");nav.className="app-sidebar-nav";
        nav.append(group("Overview",[navItem("Dashboard","/dashboard","dashboard")]),group("Analytics",[navItem("Revenue","/revenue","revenue"),navItem("Members","/members","members"),navItem("Reports","/reports","reports")]));
        if(importsAvailable)nav.append(group("Data",[navItem("Imports","/imports","imports")]));
        const settings=document.getElementById("open-settings");if(settings){const settingsNav=document.createElement("button");settingsNav.type="button";settingsNav.className="app-nav-link";settingsNav.innerHTML=`<span aria-hidden="true">${icon("settings")}</span><span>Settings</span>`;settingsNav.addEventListener("click",()=>{settings.click();closeDrawer();});nav.append(group("System",[settingsNav]));}
        const footer=document.createElement("div");footer.className="app-sidebar-footer";footer.append(themeButton());sidebar.append(brand,nav,footer);
        const mobile=document.createElement("button");mobile.type="button";mobile.className="mobile-nav-toggle";mobile.setAttribute("aria-controls","app-sidebar");mobile.setAttribute("aria-expanded","false");mobile.setAttribute("aria-label","Open navigation");mobile.innerHTML='<span aria-hidden="true">☰</span>';
        const backdrop=document.createElement("button");backdrop.type="button";backdrop.className="app-sidebar-backdrop";backdrop.setAttribute("aria-label","Close navigation");
        const closeDrawer=()=>{document.body.classList.remove("nav-open");mobile.setAttribute("aria-expanded","false");};
        mobile.addEventListener("click",()=>{const open=document.body.classList.toggle("nav-open");mobile.setAttribute("aria-expanded",String(open));if(open)sidebar.querySelector("a,button")?.focus();});backdrop.addEventListener("click",closeDrawer);sidebar.addEventListener("click",event=>{if(event.target.closest("a"))closeDrawer();});document.addEventListener("keydown",event=>{if(event.key==="Escape")closeDrawer();});
        header.prepend(mobile);document.body.prepend(sidebar,backdrop);
    });
})();
