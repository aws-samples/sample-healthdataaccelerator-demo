import React, { useState, useEffect, useContext } from 'react'
/*
 * PHI / HIPAA NOTICE:
 * This component renders protected health information (PHI) - patient name,
 * date of birth, age, and gender - fetched from a FHIR API. If you display real
 * PHI, this is a HIPAA-regulated workload: execute an AWS Business Associate
 * Addendum (BAA), keep data within HIPAA-eligible services, and enable
 * encryption, access logging, and audit controls. The customer is responsible
 * for compliant handling of regulated data. This sample uses synthetic data only.
 */
import { useNavigate } from 'react-router-dom'
import { AppContext } from '../App'

function PatientList() {
  const { API_BASE, authToken } = useContext(AppContext)
  const [patients, setPatients] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [search, setSearch] = useState('')
  const navigate = useNavigate()

  useEffect(() => {
    fetchPatients()
  }, [])

  const fetchPatients = async () => {
    try {
      setLoading(true)
      const headers = authToken ? { Authorization: `Bearer ${authToken}` } : {}
      const response = await fetch(`${API_BASE}/patients`, { headers })
      if (!response.ok) throw new Error('Failed to fetch patients')
      const data = await response.json()
      
      // Extract patients from FHIR Bundle
      const patientList = (data.entry || [])
        .map(e => e.resource)
        .filter(r => r.resourceType === 'Patient')
      
      setPatients(patientList)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const getPatientName = (patient) => {
    const name = patient.name?.[0]
    if (!name) return 'Unknown'
    const given = name.given?.join(' ') || ''
    const family = name.family || ''
    return `${given} ${family}`.trim() || 'Unknown'
  }

  const getPatientAge = (patient) => {
    if (!patient.birthDate) return null
    const birth = new Date(patient.birthDate)
    const today = new Date()
    let age = today.getFullYear() - birth.getFullYear()
    const m = today.getMonth() - birth.getMonth()
    if (m < 0 || (m === 0 && today.getDate() < birth.getDate())) {
      age--
    }
    return age
  }

  const filteredPatients = patients.filter(patient => {
    const name = getPatientName(patient).toLowerCase()
    return name.includes(search.toLowerCase())
  })

  if (loading) {
    return (
      <div className="loading">
        <div className="spinner"></div>
        Loading patients...
      </div>
    )
  }

  if (error) {
    return (
      <div className="error">
        <p>Error: {error}</p>
        <button onClick={fetchPatients}>Retry</button>
      </div>
    )
  }

  return (
    <div>
      <div className="search-bar">
        <input
          type="text"
          className="search-input"
          placeholder="Search patients..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      
      <p style={{ marginBottom: '1rem', color: '#6b7280' }}>
        {filteredPatients.length} patients found
      </p>
      
      <div className="patient-list">
        {filteredPatients.map(patient => (
          <div
            key={patient.id}
            className="patient-card"
            onClick={() => navigate(`/patient/${patient.id}`)}
          >
            <div className="patient-name">{getPatientName(patient)}</div>
            <div className="patient-info">
              {patient.birthDate && <span>DOB: {patient.birthDate}</span>}
              {getPatientAge(patient) && <span>Age: {getPatientAge(patient)}</span>}
              {patient.gender && <span>{patient.gender}</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default PatientList
