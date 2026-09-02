import { useState, useContext, useEffect } from 'react'
import { AppContext } from '../App'

// Social determinant keywords to filter out from medical conditions
const SOCIAL_DETERMINANT_KEYWORDS = [
  'employment', 'education', 'housing', 'military', 'stress', 'social isolation',
  'part-time', 'full-time', 'unemployed', 'retired', 'received higher', 'reports of violence',
  'victim of intimate partner abuse', 'misuses drugs', 'unhealthy alcohol drinking behavior',
  'lack of access', 'limited social contact', 'not in labor force'
]

function isSocialDeterminant(condition) {
  const display = (condition.code?.coding?.[0]?.display || condition.code?.text || '').toLowerCase()
  return SOCIAL_DETERMINANT_KEYWORDS.some(keyword => display.includes(keyword))
}

function PatientDashboard() {
  const { API_BASE, ORTHANC_URL, authToken } = useContext(AppContext)
  
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState([])
  const [selectedPatient, setSelectedPatient] = useState(null)
  const [patientData, setPatientData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [searching, setSearching] = useState(false)
  
  // AI state
  const [aiSummary, setAiSummary] = useState('')
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [chatMessages, setChatMessages] = useState([])
  const [chatInput, setChatInput] = useState('')
  const [chatLoading, setChatLoading] = useState(false)
  const [showChat, setShowChat] = useState(false)
  const [expandedNote, setExpandedNote] = useState(null)

  const fetchWithAuth = async (url, options = {}) => {
    const headers = { 
      'Content-Type': 'application/json',
      ...(options.headers || {})
    }
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`
    }
    const response = await fetch(url, { ...options, headers })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    return response.json()
  }

  const searchPatients = async () => {
    if (!searchQuery.trim()) return
    
    setSearching(true)
    setSearchResults([])
    setSelectedPatient(null)
    setPatientData(null)
    
    try {
      const data = await fetchWithAuth(`${API_BASE}/patients?name=${encodeURIComponent(searchQuery)}`)
      const patients = (data.entry || []).map(e => e.resource).filter(r => r.resourceType === 'Patient')
      setSearchResults(patients)
    } catch (err) {
      console.error('Search error:', err)
    } finally {
      setSearching(false)
    }
  }

  const selectPatient = async (patient) => {
    setSelectedPatient(patient)
    setLoading(true)
    setSearchResults([])
    
    try {
      // Get patient name for Orthanc query
      const patientName = getPatientName(patient)
      const nameParts = patientName.split(' ')
      const orthancName = nameParts.length > 1 
        ? `${nameParts[nameParts.length-1]}^${nameParts[0]}`
        : nameParts[0]

      // Fetch all data in parallel
      const [conditions, allergies, medications, encounters, immunizations, imagingRes, clinicalNotes, healthlakeImaging] = await Promise.all([
        fetchWithAuth(`${API_BASE}/patients/${patient.id}/conditions`),
        fetchWithAuth(`${API_BASE}/patients/${patient.id}/allergies`),
        fetchWithAuth(`${API_BASE}/patients/${patient.id}/medications`),
        fetchWithAuth(`${API_BASE}/patients/${patient.id}/encounters`),
        fetchWithAuth(`${API_BASE}/patients/${patient.id}/immunizations`),
        fetchWithAuth(`${API_BASE}/orthanc-imaging?name=${encodeURIComponent(orthancName)}`),
        fetchWithAuth(`${API_BASE}/patients/${patient.id}/notes`),
        fetchWithAuth(`${API_BASE}/imaging?patient=${patient.id}`),
      ])

      const extractResources = (data) => (data.entry || []).map(e => e.resource)
      const allConditions = extractResources(conditions)

      // Separate medical conditions from social determinants
      const medicalConditions = allConditions.filter(c => !isSocialDeterminant(c))
      const socialDeterminants = allConditions.filter(c => isSocialDeterminant(c))

      // Sort conditions: active first, then by name
      const sortConditions = (list) => list.sort((a, b) => {
        const aActive = a.clinicalStatus?.coding?.[0]?.code === 'active'
        const bActive = b.clinicalStatus?.coding?.[0]?.code === 'active'
        if (aActive && !bActive) return -1
        if (!aActive && bActive) return 1
        return 0
      })

      // Sort encounters by date descending
      const sortedEncounters = extractResources(encounters).sort((a, b) => {
        const aDate = a.period?.start || ''
        const bDate = b.period?.start || ''
        return bDate.localeCompare(aDate)
      })

      // Combine imaging from Orthanc and HealthLake
      const orthancStudies = Array.isArray(imagingRes) ? imagingRes : []
      const healthlakeStudies = extractResources(healthlakeImaging).map(study => ({
        studyUid: study.identifier?.find(i => i.system === 'urn:dicom:uid')?.value?.replace('urn:oid:', '') || study.id,
        description: study.description || study.modality?.[0]?.display || 'Imaging Study',
        modality: study.modality?.[0]?.code,
        date: study.started?.slice(0, 10),
        source: 'healthlake'
      }))
      const allImaging = [...orthancStudies.map(s => ({...s, source: 'orthanc'})), ...healthlakeStudies]

      setPatientData({
        conditions: sortConditions(medicalConditions),
        socialDeterminants: sortConditions(socialDeterminants),
        allergies: extractResources(allergies),
        medications: extractResources(medications),
        encounters: sortedEncounters,
        immunizations: extractResources(immunizations),
        imaging: allImaging,
        clinicalNotes: extractResources(clinicalNotes),
      })

      // Fetch AI summary
      const dataForAi = {
        conditions: sortConditions(medicalConditions),
        allergies: extractResources(allergies),
        medications: extractResources(medications),
        encounters: sortedEncounters,
        immunizations: extractResources(immunizations),
        imaging: allImaging,
        clinicalNotes: extractResources(clinicalNotes),
      }
      fetchAiSummary(patient, dataForAi)
    } catch (err) {
      console.error('Error fetching patient data:', err)
    } finally {
      setLoading(false)
    }
  }

  const handleKeyPress = (e) => {
    if (e.key === 'Enter') searchPatients()
  }

  const clearSearch = () => {
    setSearchQuery('')
    setSearchResults([])
    setSelectedPatient(null)
    setPatientData(null)
    setAiSummary('')
    setChatMessages([])
    setShowChat(false)
  }

  // Fetch AI summary when patient data is loaded
  const fetchAiSummary = async (patient, data) => {
    setSummaryLoading(true)
    setAiSummary('')
    try {
      const payload = {
        patientData: {
          patient,
          conditions: data.conditions,
          allergies: data.allergies,
          medications: data.medications,
          encounters: data.encounters,
          immunizations: data.immunizations,
          imaging: data.imaging,
          clinicalNotes: data.clinicalNotes,
        }
      }
      const result = await fetchWithAuth(`${API_BASE}/patient-summary`, {
        method: 'POST',
        body: JSON.stringify(payload)
      })
      setAiSummary(result.summary)
    } catch (err) {
      console.error('AI summary error:', err)
      setAiSummary('Unable to generate summary.')
    } finally {
      setSummaryLoading(false)
    }
  }

  // Send chat message
  const sendChatMessage = async () => {
    if (!chatInput.trim() || chatLoading) return
    
    const question = chatInput.trim()
    setChatInput('')
    setChatMessages(prev => [...prev, { role: 'user', content: question }])
    setChatLoading(true)
    
    try {
      const payload = {
        patientData: {
          patient: selectedPatient,
          conditions: patientData.conditions,
          allergies: patientData.allergies,
          medications: patientData.medications,
          encounters: patientData.encounters,
          immunizations: patientData.immunizations,
          imaging: patientData.imaging,
          clinicalNotes: patientData.clinicalNotes,
        },
        question,
        chatHistory: chatMessages.filter(m => m.role === 'user').map((m, i) => ({
          question: m.content,
          answer: chatMessages[chatMessages.findIndex((_, idx) => chatMessages[idx] === m) + 1]?.content || ''
        })).slice(-3)
      }
      const result = await fetchWithAuth(`${API_BASE}/patient-chat`, {
        method: 'POST',
        body: JSON.stringify(payload)
      })
      setChatMessages(prev => [...prev, { role: 'assistant', content: result.answer }])
    } catch (err) {
      console.error('Chat error:', err)
      setChatMessages(prev => [...prev, { role: 'assistant', content: 'Sorry, I encountered an error.' }])
    } finally {
      setChatLoading(false)
    }
  }

  const handleChatKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendChatMessage()
    }
  }

  const getPatientName = (patient) => {
    if (!patient?.name?.[0]) return 'Unknown'
    const name = patient.name[0]
    return `${name.given?.join(' ') || ''} ${name.family || ''}`.trim()
  }

  const getPatientAge = (patient) => {
    if (!patient?.birthDate) return null
    const birth = new Date(patient.birthDate)
    const today = new Date()
    let age = today.getFullYear() - birth.getFullYear()
    const m = today.getMonth() - birth.getMonth()
    if (m < 0 || (m === 0 && today.getDate() < birth.getDate())) age--
    return age
  }

  const openImageViewer = (study) => {
    if (study.studyUid) {
      window.open(`${ORTHANC_URL}/ohif/viewer?StudyInstanceUIDs=${study.studyUid}`, '_blank')
    } else {
      window.open(ORTHANC_URL, '_blank')
    }
  }

  return (
    <div className="dashboard">
      {/* Search Section */}
      <div className="search-section">
        <div className="search-container">
          <input
            type="text"
            className="search-input"
            placeholder="Search patient by name..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyPress={handleKeyPress}
          />
          <button className="search-btn" onClick={searchPatients} disabled={searching}>
            {searching ? '...' : '🔍'}
          </button>
          {(selectedPatient || searchResults.length > 0) && (
            <button className="clear-btn" onClick={clearSearch}>Clear</button>
          )}
        </div>

        {/* Search Results Dropdown */}
        {searchResults.length > 0 && (
          <div className="search-results">
            {searchResults.map(patient => (
              <div
                key={patient.id}
                className="search-result-item"
                onClick={() => selectPatient(patient)}
              >
                <span className="result-name">{getPatientName(patient)}</span>
                <span className="result-info">
                  {patient.birthDate} • {patient.gender}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Loading State */}
      {loading && (
        <div className="loading">
          <div className="spinner"></div>
          Loading patient data...
        </div>
      )}

      {/* Patient Dashboard */}
      {selectedPatient && patientData && !loading && (
        <div className="patient-dashboard">
          {/* Patient Header */}
          <div className="patient-header">
            <div className="patient-avatar">
              {getPatientName(selectedPatient).split(' ').map(n => n[0]).join('').slice(0, 2)}
            </div>
            <div className="patient-info-header">
              <h2>{getPatientName(selectedPatient)}</h2>
              <div className="patient-meta-row">
                {selectedPatient.birthDate && <span>DOB: {selectedPatient.birthDate}</span>}
                {getPatientAge(selectedPatient) && <span>Age: {getPatientAge(selectedPatient)}</span>}
                {selectedPatient.gender && <span>{selectedPatient.gender}</span>}
                {selectedPatient.identifier?.find(i => i.type?.coding?.[0]?.code === 'SS' || i.system?.includes('ssn')) && (
                  <span>SSN: ***-**-{selectedPatient.identifier.find(i => i.type?.coding?.[0]?.code === 'SS' || i.system?.includes('ssn')).value?.slice(-4) || '****'}</span>
                )}
              </div>
              <div className="patient-meta-row">
                {selectedPatient.telecom?.find(t => t.system === 'phone') && (
                  <span>📞 {selectedPatient.telecom.find(t => t.system === 'phone').value}</span>
                )}
                {selectedPatient.telecom?.find(t => t.system === 'email') && (
                  <span>✉️ {selectedPatient.telecom.find(t => t.system === 'email').value}</span>
                )}
              </div>
              <div className="patient-meta-row">
                {selectedPatient.address?.[0] && (
                  <span>📍 {[
                    selectedPatient.address[0].line?.join(', '),
                    selectedPatient.address[0].city,
                    selectedPatient.address[0].state,
                    selectedPatient.address[0].postalCode,
                    selectedPatient.address[0].country
                  ].filter(Boolean).join(', ')}</span>
                )}
              </div>
            </div>
            <button 
              className="chat-toggle-btn"
              onClick={() => setShowChat(!showChat)}
            >
              {showChat ? '✕ Close Chat' : '💬 Ask AI'}
            </button>
          </div>

          {/* AI Summary */}
          <div className="ai-summary-section">
            <div className="ai-summary-header">
              <span>🤖</span>
              <h3>AI Summary</h3>
            </div>
            <div className="ai-summary-content">
              {summaryLoading ? (
                <div className="ai-loading">Generating summary...</div>
              ) : (
                <p>{aiSummary || 'No summary available.'}</p>
              )}
            </div>
          </div>

          {/* Chat Panel */}
          {showChat && (
            <div className="chat-panel">
              <div className="chat-messages">
                {chatMessages.length === 0 && (
                  <div className="chat-empty">Ask questions about this patient's data</div>
                )}
                {chatMessages.map((msg, i) => (
                  <div key={i} className={`chat-message ${msg.role}`}>
                    <div className="chat-bubble">{msg.content}</div>
                  </div>
                ))}
                {chatLoading && (
                  <div className="chat-message assistant">
                    <div className="chat-bubble">Thinking...</div>
                  </div>
                )}
              </div>
              <div className="chat-input-area">
                <input
                  type="text"
                  className="chat-input"
                  placeholder="Ask about this patient..."
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  onKeyDown={handleChatKeyDown}
                  disabled={chatLoading}
                />
                <button 
                  className="chat-send-btn" 
                  onClick={sendChatMessage}
                  disabled={chatLoading || !chatInput.trim()}
                >
                  Send
                </button>
              </div>
            </div>
          )}

          {/* Dashboard Grid */}
          <div className="dashboard-grid">
            {/* Medical Conditions */}
            <Section title="Medical Conditions" icon="🩺" count={patientData.conditions.length}>
              {patientData.conditions.length === 0 ? (
                <p className="empty">No medical conditions recorded</p>
              ) : (
                patientData.conditions.map((c, i) => (
                  <div key={i} className="item">
                    <span className="item-name">{c.code?.coding?.[0]?.display || c.code?.text || 'Unknown'}</span>
                    <span className={`badge ${c.clinicalStatus?.coding?.[0]?.code === 'active' ? 'active' : 'resolved'}`}>
                      {c.clinicalStatus?.coding?.[0]?.code || 'unknown'}
                    </span>
                  </div>
                ))
              )}
            </Section>

            {/* Allergies */}
            <Section title="Allergies" icon="⚠️" count={patientData.allergies.length}>
              {patientData.allergies.length === 0 ? (
                <p className="empty">No allergies recorded</p>
              ) : (
                patientData.allergies.map((a, i) => (
                  <div key={i} className="item">
                    <span className="item-name">{a.code?.coding?.[0]?.display || a.code?.text || 'Unknown'}</span>
                    {a.criticality === 'high' && <span className="badge high">High</span>}
                  </div>
                ))
              )}
            </Section>

            {/* Medications */}
            <Section title="Medications" icon="💊" count={patientData.medications.length}>
              {patientData.medications.length === 0 ? (
                <p className="empty">No medications recorded</p>
              ) : (
                patientData.medications.map((m, i) => (
                  <div key={i} className="item">
                    <span className="item-name">
                      {m.medicationCodeableConcept?.coding?.[0]?.display || m.medicationCodeableConcept?.text || 'Unknown'}
                    </span>
                  </div>
                ))
              )}
            </Section>

            {/* Immunizations */}
            <Section title="Immunizations" icon="💉" count={patientData.immunizations.length}>
              {patientData.immunizations.length === 0 ? (
                <p className="empty">No immunizations recorded</p>
              ) : (
                patientData.immunizations.map((im, i) => (
                  <div key={i} className="item">
                    <span className="item-name">{im.vaccineCode?.coding?.[0]?.display || 'Unknown'}</span>
                    {im.occurrenceDateTime && (
                      <span className="item-date">{new Date(im.occurrenceDateTime).toLocaleDateString()}</span>
                    )}
                  </div>
                ))
              )}
            </Section>

            {/* Encounters */}
            <Section title="Recent Encounters" icon="📋" count={patientData.encounters.length}>
              {patientData.encounters.length === 0 ? (
                <p className="empty">No encounters recorded</p>
              ) : (
                patientData.encounters.slice(0, 10).map((e, i) => (
                  <div key={i} className="item">
                    <span className="item-name">
                      {e.type?.[0]?.coding?.[0]?.display || e.class?.display || e.class?.code || 'Visit'}
                    </span>
                    {e.period?.start && (
                      <span className="item-date">{new Date(e.period.start).toLocaleDateString()}</span>
                    )}
                  </div>
                ))
              )}
            </Section>

            {/* Imaging */}
            <Section title="Imaging Studies" icon="🔬" count={patientData.imaging.length}>
              {patientData.imaging.length === 0 ? (
                <p className="empty">No imaging studies</p>
              ) : (
                patientData.imaging.map((study, i) => (
                  <div key={i} className="imaging-item">
                    <div className="imaging-info">
                      <span className="item-name">{study.description || study.modality || 'Study'}</span>
                      {study.date && (
                        <span className="item-date">
                          {study.date.slice(0,4)}-{study.date.slice(4,6)}-{study.date.slice(6,8)}
                        </span>
                      )}
                    </div>
                    <button className="view-btn" onClick={() => openImageViewer(study)}>
                      View
                    </button>
                  </div>
                ))
              )}
            </Section>

            {/* Clinical Notes */}
            <Section title="Clinical Notes" icon="📝" count={patientData.clinicalNotes?.length || 0}>
              {(!patientData.clinicalNotes || patientData.clinicalNotes.length === 0) ? (
                <p className="empty">No clinical notes recorded</p>
              ) : (
                patientData.clinicalNotes.map((note, i) => {
                  const noteType = note.type?.text || note.type?.coding?.[0]?.display || 'Clinical Note'
                  const noteDate = note.date ? note.date.substring(0, 10) : 'Unknown date'
                  const noteText = note.content?.[0]?.attachment?.decodedText || ''
                  const isExpanded = expandedNote === i
                  
                  return (
                    <div key={i} className="item" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
                      <div 
                        onClick={() => setExpandedNote(isExpanded ? null : i)}
                        style={{ cursor: 'pointer', display: 'flex', justifyContent: 'space-between', alignItems: 'center', width: '100%' }}
                      >
                        <span className="item-name">
                          {noteType}
                          <span className="badge" style={{ background: '#e0f2fe', color: '#0369a1', marginLeft: '0.5rem' }}>{noteDate}</span>
                        </span>
                        <span style={{ fontSize: '0.75rem', color: '#6b7280' }}>
                          {isExpanded ? '▼' : '▶'}
                        </span>
                      </div>
                      {isExpanded && noteText && (
                        <div style={{
                          marginTop: '0.75rem',
                          padding: '1rem',
                          backgroundColor: '#f9fafb',
                          borderRadius: '0.5rem',
                          whiteSpace: 'pre-wrap',
                          fontFamily: 'monospace',
                          fontSize: '0.8rem',
                          lineHeight: '1.5',
                          maxHeight: '300px',
                          overflowY: 'auto',
                          border: '1px solid #e5e7eb'
                        }}>
                          {noteText}
                        </div>
                      )}
                    </div>
                  )
                })
              )}
            </Section>

          </div>
        </div>
      )}

      {/* Empty State */}
      {!selectedPatient && !loading && searchResults.length === 0 && (
        <div className="empty-state">
          <div className="empty-icon">🔍</div>
          <h3>Search for a Patient</h3>
          <p>Enter a patient name to view their health records</p>
        </div>
      )}
    </div>
  )
}

function Section({ title, icon, count, children }) {
  return (
    <div className="section-card">
      <div className="section-header">
        <span>{icon}</span>
        <h3>{title}</h3>
        {count > 0 && <span className="count">({count})</span>}
      </div>
      <div className="section-body">{children}</div>
    </div>
  )
}

export default PatientDashboard
