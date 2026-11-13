import * as React from "react"
import { cn } from "../../lib/utils"
import { motion, useReducedMotion } from "framer-motion"

const Button = React.forwardRef(({ className, variant = "default", size = "default", whileTap = { scale: 0.95 }, ...props }, ref) => {
    const reduceMotion = useReducedMotion()
    // ... variants and sizes definitions ...
    const variants = {
        default: "bg-blue-600 text-white hover:bg-blue-700 shadow-sm",
        secondary: "bg-gray-100 text-text hover:bg-gray-200",
        outline: "border border-gray-200 bg-white hover:bg-gray-100 text-text",
        ghost: "hover:bg-gray-100 text-text",
        link: "text-primary underline-offset-4 hover:underline",
    }

    const sizes = {
        default: "h-11 px-4 py-2",
        sm: "h-9 px-3",
        lg: "h-12 px-8",
        icon: "h-11 w-11",
    }

    return (
        <motion.button
            whileTap={reduceMotion ? undefined : whileTap}
            className={cn(
                "inline-flex items-center justify-center whitespace-nowrap rounded-xl text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50",
                variants[variant],
                sizes[size],
                className
            )}
            ref={ref}
            {...props}
        />
    )
})
Button.displayName = "Button"

export { Button }
