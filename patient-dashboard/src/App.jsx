import { useState, createContext } from 'react'
import PatientDashboard from './components/PatientDashboard'
import Login from './components/Login'

// API base URL
// ORTHANC_URL is injected at deploy time via window.ORTHANC_URL; no endpoint
// is hardcoded here.
const API_BASE = window.API_BASE_URL || '/api'
const ORTHANC_URL = window.ORTHANC_URL || ''
const COGNITO_CONFIG = window.COGNITO_CONFIG || null

export const AppContext = createContext({ API_BASE, ORTHANC_URL })

function App() {
  // Check for existing token in sessionStorage on initial load
  const [authToken, setAuthToken] = useState(() => {
    return sessionStorage.getItem('authToken')
  })
  const [isAuthenticated, setIsAuthenticated] = useState(() => {
    return !!sessionStorage.getItem('authToken')
  })

  const handleLogin = (token) => {
    sessionStorage.setItem('authToken', token)
    setAuthToken(token)
    setIsAuthenticated(true)
  }

  const handleLogout = () => {
    sessionStorage.removeItem('authToken')
    setAuthToken(null)
    setIsAuthenticated(false)
  }

  // If Cognito is not configured, skip authentication
  const requiresAuth = COGNITO_CONFIG !== null

  if (requiresAuth && !isAuthenticated) {
    return <Login onLogin={handleLogin} config={COGNITO_CONFIG} />
  }

  return (
    <AppContext.Provider value={{ API_BASE, ORTHANC_URL, authToken }}>
      <div className="app">
        <header className="header">
          <h1>🏥 Patient 360 Dashboard</h1>
          {requiresAuth && (
            <button className="logout-btn" onClick={handleLogout}>Logout</button>
          )}
        </header>
        <main className="main-content">
          <PatientDashboard />
        </main>
      </div>
    </AppContext.Provider>
  )
}

export default App
