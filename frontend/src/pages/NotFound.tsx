import { Link } from "react-router-dom"

export default function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center py-24 text-center">
      <h1 className="text-4xl font-bold text-slate-200">404</h1>
      <p className="mt-2 text-slate-400">Page not found.</p>
      <Link to="/" className="mt-6 rounded-md bg-info px-4 py-2 text-sm font-medium text-white hover:bg-blue-600">
        Back to Command Center
      </Link>
    </div>
  )
}
