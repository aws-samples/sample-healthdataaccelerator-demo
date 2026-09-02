import React, { useState, useEffect, useContext } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { AppContext } from '../App'

function PatientDetail() {
  const { patientId } = useParams()
  const navigate = useNavigate()
  const { API_BASE, ORTHANC_URL, authToken } = useContext(AppContext)
  
  const [patient, setPatient] = useState(null)
  const [conditions, setConditions] = useState([])
  const [allergies, setAllergies] = useState([])
  const [medications, setMedications] = useState([])
  const [encounters, setEncounters] = useState([])
  const [immunizations, setImmunizations] = useState([])
  const [imaging, setImaging] = useState([])
  const [clinicalNotes, setClinicalNotes] = useState([])
  const [loading, setLoading] = useState(true)
  const [expandedNote, setExpandedNote] = useState(null)

  const fetchWithAuth = async (url) => {
    const headers = { 'Content-Type': 'application/json' }
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`
    }
    return fetch(url, { headers })
  }

  useEffect(() => {
    fetchAllData()
  }, [patientId])

  const fetchAllData = async () => {
    setLoading(true)
    try {
      // Fetch all data in parallel
      const [
        patientRes,
        conditionsRes,
        allergiesRes,
        medicationsRes,
        encountersRes,
        immunizationsRes,
        imagingRes,
        notesRes,
      ] = await Promise.all([
        fetchWithAuth(`${API_BASE}/patients/${patientId}`),
        fetchWithAuth(`${API_BASE}/patients/${patientId}/conditions`),
        fetchWithAuth(`${API_BASE}/patients/${patientId}/allergies`),
        fetchWithAuth(`${API_BASE}/patients/${patientId}/medications`),
        fetchWithAuth(`${API_BASE}/patients/${patientId}/encounters`),
        fetchWithAuth(`${API_BASE}/patients/${patientId}/immunizations`),
        fetchWithAuth(`${API_BASE}/imaging?patient=${patientId}`),
        fetchWithAuth(`${API_BASE}/patients/${patientId}/notes`),
      ])

      const patientData = await patientRes.json()
      setPatient(patientData)

      // Extract resources from FHIR Bundles
      const extractResources = async (res) => {
        const data = await res.json()
        return (data.entry || []).map(e => e.resource)
      }

      setConditions(await extractResources(conditionsRes))
      setAllergies(await extractResources(allergiesRes))
      setMedications(await extractResources(medicationsRes))
      setEncounters(await extractResources(encountersRes))
      setImmunizations(await extractResources(immunizationsRes))
      setImaging(await extractResources(imagingRes))
      setClinicalNotes(await extractResources(notesRes))
    } catch (err) {
      console.error('Error fetching data:', err)
    } finally {
      setLoading(false)
    }
  }

  const getPatientName = (patient) => {
    if (!patient?.name?.[0]) return 'Unknown'
    const name = patient.name[0]
    const given = name.given?.join(' ') || ''
    const family = name.family || ''
    return `${given} ${family}`.trim()
  }

  const getInitials = (name) => {
    return name.split(' ').map(n => n[0]).join('').toUpperCase().slice(0, 2)
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
    // Get study UID from the ImagingStudy resource
    const studyUid = study.identifier?.find(i => 
      i.system === 'urn:dicom:uid' || i.type?.coding?.[0]?.code === 'ACSN'
    )?.value?.replace('urn:oid:', '')
    
    if (studyUid) {
      // Open in Orthanc's OHIF viewer
      window.open(`${ORTHANC_URL}/ohif/viewer?StudyInstanceUIDs=${studyUid}`, '_blank')
    } else {
      // Fallback: open Orthanc main page
      window.open(ORTHANC_URL, '_blank')
    }
  }

  if (loading) {
    return (
      <div className="loading">
        <div className="spinner"></div>
        Loading patient data...
      </div>
    )
  }

  const patientName = getPatientName(patient)

  return (
    <div>
      <button className="back-button" onClick={() => navigate('/')}>
        ← Back to Patient List
      </button>

      <div className="patient-detail">
        {/* Sidebar */}
        <div className="patient-sidebar">
          <div className="patient-avatar">{getInitials(patientName)}</div>
          <h2>{patientName}</h2>
          <div className="patient-meta">
            {patient?.birthDate && (
              <p><strong>DOB:</strong> {patient.birthDate}</p>
            )}
            {getPatientAge(patient) && (
              <p><strong>Age:</strong> {getPatientAge(patient)} years</p>
            )}
            {patient?.gender && (
              <p><strong>Gender:</strong> {patient.gender.charAt(0).toUpperCase() + patient.gender.slice(1)}</p>
            )}
            {patient?.maritalStatus?.coding?.[0]?.display && (
              <p><strong>Marital Status:</strong> {patient.maritalStatus.coding[0].display}</p>
            )}
            {patient?.communication?.[0]?.language?.coding?.[0]?.display && (
              <p><strong>Language:</strong> {patient.communication[0].language.coding[0].display}</p>
            )}
            {patient?.identifier?.find(i => i.type?.coding?.[0]?.code === 'SS' || i.system?.includes('ssn')) && (
              <p><strong>SSN:</strong> ***-**-{patient.identifier.find(i => i.type?.coding?.[0]?.code === 'SS' || i.system?.includes('ssn')).value?.slice(-4) || '****'}</p>
            )}
            {patient?.identifier?.find(i => i.type?.coding?.[0]?.code === 'MR' || i.system?.includes('mrn')) && (
              <p><strong>MRN:</strong> {patient.identifier.find(i => i.type?.coding?.[0]?.code === 'MR' || i.system?.includes('mrn')).value}</p>
            )}
          </div>
          
          <div className="patient-meta" style={{ marginTop: '1rem', paddingTop: '1rem', borderTop: '1px solid #e5e7eb' }}>
            <h4 style={{ margin: '0 0 0.5rem 0', fontSize: '0.875rem', color: '#6b7280' }}>Contact Information</h4>
            {patient?.address?.[0] && (
              <p><strong>Address:</strong><br/>{
                [patient.address[0].line?.join(', '), 
                 patient.address[0].city, 
                 patient.address[0].state,
                 patient.address[0].postalCode,
                 patient.address[0].country].filter(Boolean).join(', ')
              }</p>
            )}
            {patient?.telecom?.find(t => t.system === 'phone') && (
              <p><strong>Phone:</strong> {patient.telecom.find(t => t.system === 'phone').value}
                {patient.telecom.find(t => t.system === 'phone').use && 
                  ` (${patient.telecom.find(t => t.system === 'phone').use})`}
              </p>
            )}
            {patient?.telecom?.find(t => t.system === 'email') && (
              <p><strong>Email:</strong> {patient.telecom.find(t => t.system === 'email').value}</p>
            )}
          </div>

          {(patient?.contact?.length > 0) && (
            <div className="patient-meta" style={{ marginTop: '1rem', paddingTop: '1rem', borderTop: '1px solid #e5e7eb' }}>
              <h4 style={{ margin: '0 0 0.5rem 0', fontSize: '0.875rem', color: '#6b7280' }}>Emergency Contact</h4>
              {patient.contact.map((contact, i) => (
                <div key={i}>
                  {contact.name && (
                    <p><strong>Name:</strong> {
                      [contact.name.given?.join(' '), contact.name.family].filter(Boolean).join(' ')
                    }</p>
                  )}
                  {contact.relationship?.[0]?.coding?.[0]?.display && (
                    <p><strong>Relationship:</strong> {contact.relationship[0].coding[0].display}</p>
                  )}
                  {contact.telecom?.find(t => t.system === 'phone') && (
                    <p><strong>Phone:</strong> {contact.telecom.find(t => t.system === 'phone').value}</p>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Main Content */}
        <div className="sections">
          {/* Conditions */}
          <Section title="Medical Conditions" icon="🩺" count={conditions.length}>
            {conditions.length === 0 ? (
              <p className="section-empty">No conditions recorded</p>
            ) : (
              conditions.map((condition, i) => (
                <div key={i} className="item">
                  <div className="item-title">
                    {condition.code?.coding?.[0]?.display || condition.code?.text || 'Unknown condition'}
                    <span className={`item-badge ${condition.clinicalStatus?.coding?.[0]?.code === 'active' ? 'badge-active' : 'badge-resolved'}`}>
                      {condition.clinicalStatus?.coding?.[0]?.code || 'unknown'}
                    </span>
                  </div>
                  {condition.onsetDateTime && (
                    <div className="item-subtitle">Onset: {new Date(condition.onsetDateTime).toLocaleDateString()}</div>
                  )}
                </div>
              ))
            )}
          </Section>

          {/* Allergies */}
          <Section title="Allergies" icon="⚠️" count={allergies.length}>
            {allergies.length === 0 ? (
              <p className="section-empty">No allergies recorded</p>
            ) : (
              allergies.map((allergy, i) => (
                <div key={i} className="item">
                  <div className="item-title">
                    {allergy.code?.coding?.[0]?.display || allergy.code?.text || 'Unknown allergen'}
                    {allergy.criticality === 'high' && (
                      <span className="item-badge badge-high">High Risk</span>
                    )}
                  </div>
                  {allergy.reaction?.[0]?.manifestation?.[0]?.coding?.[0]?.display && (
                    <div className="item-subtitle">
                      Reaction: {allergy.reaction[0].manifestation[0].coding[0].display}
                    </div>
                  )}
                </div>
              ))
            )}
          </Section>

          {/* Medications */}
          <Section title="Medications" icon="💊" count={medications.length}>
            {medications.length === 0 ? (
              <p className="section-empty">No medications recorded</p>
            ) : (
              medications.map((med, i) => (
                <div key={i} className="item">
                  <div className="item-title">
                    {med.medicationCodeableConcept?.coding?.[0]?.display || 
                     med.medicationCodeableConcept?.text || 'Unknown medication'}
                  </div>
                  {med.dosageInstruction?.[0]?.text && (
                    <div className="item-subtitle">{med.dosageInstruction[0].text}</div>
                  )}
                </div>
              ))
            )}
          </Section>

          {/* Encounters */}
          <Section title="Recent Encounters" icon="📋" count={encounters.length}>
            {encounters.length === 0 ? (
              <p className="section-empty">No encounters recorded</p>
            ) : (
              encounters.slice(0, 10).map((encounter, i) => (
                <div key={i} className="item">
                  <div className="item-title">
                    {encounter.type?.[0]?.coding?.[0]?.display || 
                     encounter.class?.display || 
                     encounter.class?.code || 'Visit'}
                  </div>
                  <div className="item-subtitle">
                    {encounter.period?.start && encounter.period.start.substring(0, 10)}
                    {encounter.status && ` • ${encounter.status}`}
                  </div>
                </div>
              ))
            )}
          </Section>

          {/* Clinical Notes */}
          <Section title="Clinical Notes" icon="📝" count={clinicalNotes.length}>
            {clinicalNotes.length === 0 ? (
              <p className="section-empty">No clinical notes recorded</p>
            ) : (
              clinicalNotes.map((note, i) => {
                const noteType = note.type?.text || note.type?.coding?.[0]?.display || 'Clinical Note'
                const noteDate = note.date ? note.date.substring(0, 10) : 'Unknown date'
                const noteText = note.content?.[0]?.attachment?.decodedText || ''
                const isExpanded = expandedNote === i
                
                return (
                  <div key={i} className="item clinical-note-item">
                    <div 
                      className="item-title clinical-note-header"
                      onClick={() => setExpandedNote(isExpanded ? null : i)}
                      style={{ cursor: 'pointer', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}
                    >
                      <span>
                        {noteType}
                        <span className="item-badge badge-note">{noteDate}</span>
                      </span>
                      <span style={{ fontSize: '0.75rem', color: '#6b7280' }}>
                        {isExpanded ? '▼ Collapse' : '▶ Expand'}
                      </span>
                    </div>
                    {isExpanded && noteText && (
                      <div className="clinical-note-content" style={{
                        marginTop: '0.75rem',
                        padding: '1rem',
                        backgroundColor: '#f9fafb',
                        borderRadius: '0.5rem',
                        whiteSpace: 'pre-wrap',
                        fontFamily: 'monospace',
                        fontSize: '0.8rem',
                        lineHeight: '1.5',
                        maxHeight: '400px',
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

          {/* Immunizations */}
          <Section title="Immunizations" icon="💉" count={immunizations.length}>
            {immunizations.length === 0 ? (
              <p className="section-empty">No immunizations recorded</p>
            ) : (
              immunizations.map((imm, i) => (
                <div key={i} className="item">
                  <div className="item-title">
                    {imm.vaccineCode?.coding?.[0]?.display || imm.vaccineCode?.text || 'Unknown vaccine'}
                  </div>
                  {imm.occurrenceDateTime && (
                    <div className="item-subtitle">
                      Date: {new Date(imm.occurrenceDateTime).toLocaleDateString()}
                    </div>
                  )}
                </div>
              ))
            )}
          </Section>

          {/* Imaging Studies */}
          <Section title="Imaging Studies" icon="🔬" count={imaging.length}>
            {imaging.length === 0 ? (
              <p className="section-empty">No imaging studies available</p>
            ) : (
              <div className="imaging-grid">
                {imaging.map((study, i) => (
                  <div key={i} className="imaging-card">
                    <h4>{study.description || study.modality?.[0]?.display || 'Imaging Study'}</h4>
                    <p>Modality: {study.modality?.[0]?.code || 'Unknown'}</p>
                    {study.started && <p>Date: {new Date(study.started).toLocaleDateString()}</p>}
                    {study.numberOfSeries && <p>Series: {study.numberOfSeries}</p>}
                    {study.numberOfInstances && <p>Images: {study.numberOfInstances}</p>}
                    <button 
                      className="view-image-btn"
                      onClick={() => openImageViewer(study)}
                    >
                      View Images
                    </button>
                  </div>
                ))}
              </div>
            )}
          </Section>
        </div>
      </div>
    </div>
  )
}

function Section({ title, icon, count, children }) {
  return (
    <div className="section">
      <div className="section-header">
        <span className="section-icon">{icon}</span>
        <h3>{title}</h3>
        {count > 0 && <span style={{ color: '#6b7280', fontSize: '0.875rem' }}>({count})</span>}
      </div>
      <div className="section-content">
        {children}
      </div>
    </div>
  )
}

export default PatientDetail
