import * as React from "react"
import { cn } from "../../lib/utils"

function Badge({ className, variant = "default", ...props }) {
    const variants = {
        default: "border-transparent bg-primary text-white hover:bg-primary/80",
        secondary: "border-transparent bg-gray-100 text-text hover:bg-gray-200",
        outline: "text-text",
        destructive: "border-transparent bg-red-500 text-white hover:bg-red-500/80",
    }

    return (
        <div
            className={cn(
                "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
                variants[variant],
                className
            )}
            {...props}
        />
    )
}

export { Badge }
