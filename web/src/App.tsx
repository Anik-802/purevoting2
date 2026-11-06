import { Navigate, Route, Routes } from "react-router-dom";
import VotePage from "./voter/VotePage";
import ReceiptLookup from "./voter/ReceiptLookup";
import AdminLayout from "./admin/AdminLayout";
import Login from "./admin/Login";
import ElectionList from "./admin/ElectionList";
import ElectionDetail from "./admin/ElectionDetail";
import CreateElection from "./admin/CreateElection";
import Home from "./Home";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      {/* voter web: link format /vote/<election_id>#t=<token> (core-api.openapi.yaml, redeemToken) */}
      <Route path="/vote/:electionId" element={<VotePage />} />
      <Route path="/receipt" element={<ReceiptLookup />} />
      {/* admin web */}
      <Route path="/admin/login" element={<Login />} />
      <Route path="/admin" element={<AdminLayout />}>
        <Route index element={<ElectionList />} />
        <Route path="new" element={<CreateElection />} />
        <Route path="elections/:electionId" element={<ElectionDetail />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
