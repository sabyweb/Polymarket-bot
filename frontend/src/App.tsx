import { Routes, Route } from "react-router-dom"
import Layout from "./components/Layout"
import CommandCenter from "./pages/CommandCenter"
import ABLab from "./pages/ABLab"
import PnL from "./pages/PnL"
import Positions from "./pages/Positions"
import Markets from "./pages/Markets"
import Rewards from "./pages/Rewards"
import Health from "./pages/Health"
import Config from "./pages/Config"
import NotFound from "./pages/NotFound"

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<CommandCenter />} />
        <Route path="ab" element={<ABLab />} />
        <Route path="pnl" element={<PnL />} />
        <Route path="positions" element={<Positions />} />
        <Route path="markets" element={<Markets />} />
        <Route path="rewards" element={<Rewards />} />
        <Route path="health" element={<Health />} />
        <Route path="config" element={<Config />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}
