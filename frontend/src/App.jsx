import { useEffect, useRef, useState } from "react";
import { useAuth } from "./context/AuthContext.jsx";
import LandingPage from "./components/LandingPage.jsx";
import AuthScreen from "./components/AuthScreen.jsx";
import Layout from "./components/Layout.jsx";
import Dashboard from "./components/Dashboard.jsx";
import SearchView from "./components/SearchView.jsx";
import OrganizationView from "./components/OrganizationView.jsx";
import PeopleView from "./components/PeopleView.jsx";
import CrmView from "./components/CrmView.jsx";
import PlatformView from "./components/PlatformView.jsx";

export default function App() {
  const { user, loading } = useAuth();
  const [view, setView] = useState("dashboard");
  const [searchPrefill, setSearchPrefill] = useState(null);
  const [guestScreen, setGuestScreen] = useState("landing");
  const [dashboardKey, setDashboardKey] = useState(0);
  const prevView = useRef(view);

  useEffect(() => {
    if (view === "dashboard" && prevView.current === "search") {
      setDashboardKey((k) => k + 1);
    }
    prevView.current = view;
  }, [view]);

  if (loading) {
    return (
      <div className="screen">
        <div className="empty" style={{ margin: "4rem auto", maxWidth: 320 }}>
          Loading…
        </div>
      </div>
    );
  }

  if (!user) {
    if (guestScreen === "landing") {
      return (
        <LandingPage
          onSignIn={() => setGuestScreen("auth-login")}
          onGetStarted={() => setGuestScreen("auth-register")}
        />
      );
    }
    return (
      <AuthScreen
        initialTab={guestScreen === "auth-register" ? "register" : "login"}
        onBack={() => setGuestScreen("landing")}
      />
    );
  }

  const openSearch = (searchId) => {
    setSearchPrefill({ searchId });
    setView("search");
  };

  return (
    <Layout view={view} setView={setView}>
      {view === "dashboard" && (
        <Dashboard
          key={dashboardKey}
          onOpenSearch={openSearch}
          onNewSearch={() => {
            setSearchPrefill(null);
            setView("search");
          }}
        />
      )}
      {view === "search" && (
        <SearchView
          prefill={searchPrefill}
          onClearPrefill={() => setSearchPrefill(null)}
        />
      )}
      {view === "people" && <PeopleView />}
      {view === "crm" && <CrmView />}
      {view === "org" && <OrganizationView />}
      {view === "platform" && <PlatformView />}
    </Layout>
  );
}
