import React from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';

// Pages
import Dashboard from './pages/Dashboard';
import SettlementDashboard from './pages/SettlementDashboard';
// ... import other pages

const AppRoutes = () => {
  return (
    <Router>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/settlement" element={<SettlementDashboard />} />
        {/* ... other routes ... */}
      </Routes>
    </Router>
  );
};

export default AppRoutes;
