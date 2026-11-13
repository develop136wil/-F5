import * as React from "react"
import { cn } from "../../lib/utils"

/**
 * 네이티브 title="" 툴팁 대체용 경량 컴포넌트. 새 의존성 추가 없이 Tailwind만으로 구현.
 * 사용법: <Tooltip content="설명"><button>...</button></Tooltip>
 */
const SIDE_POSITION = {
    top: "bottom-full left-1/2 -translate-x-1/2 mb-2 origin-bottom",
    bottom: "top-full left-1/2 -translate-x-1/2 mt-2 origin-top",
}
const SIDE_ARROW = {
    top: "top-full left-1/2 -translate-x-1/2 border-t-gray-800 dark:border-t-gray-700 border-x-transparent border-b-transparent",
    bottom: "bottom-full left-1/2 -translate-x-1/2 border-b-gray-800 dark:border-b-gray-700 border-x-transparent border-t-transparent",
}

const Tooltip = ({ content, children, side = "top", className, wrapperClassName }) => {
    const id = React.useId()
    const [hovered, setHovered] = React.useState(false)
    const [focused, setFocused] = React.useState(false)
    const [dismissed, setDismissed] = React.useState(false)
    const [offset, setOffset] = React.useState(0)
    const leaveTimer = React.useRef(null)
    const wrapper = React.useRef(null)
    const popup = React.useRef(null)
    const visible = Boolean(content) && (hovered || focused) && !dismissed
    React.useEffect(() => () => window.clearTimeout(leaveTimer.current), [])
    React.useLayoutEffect(() => {
        if (!visible) return undefined
        const keepInViewport = () => {
            const rect = popup.current?.getBoundingClientRect()
            if (!rect) return
            const shift = rect.left < 8 ? 8 - rect.left : Math.min(0, window.innerWidth - 8 - rect.right)
            if (shift) setOffset(value => value + shift)
        }
        keepInViewport()
        window.addEventListener('resize', keepInViewport)
        return () => window.removeEventListener('resize', keepInViewport)
    }, [visible, content, side])
    React.useEffect(() => {
        if (!visible) return undefined
        const dismiss = event => {
            if (event.key !== 'Escape' || event.defaultPrevented) return
            event.preventDefault()
            event.stopPropagation()
            setDismissed(true)
        }
        // Text-only help stays readable under the pointer without intercepting
        // clicks intended for controls underneath it.
        const trackPointer = event => {
            if (event.pointerType === 'touch') return
            const rect = popup.current?.getBoundingClientRect()
            const overPopup = rect && event.clientX >= rect.left && event.clientX <= rect.right
                && event.clientY >= rect.top && event.clientY <= rect.bottom
            window.clearTimeout(leaveTimer.current)
            if (overPopup || wrapper.current?.contains(event.target)) setHovered(true)
            else leaveTimer.current = window.setTimeout(() => setHovered(false), 150)
        }
        document.addEventListener('keydown', dismiss, true)
        document.addEventListener('pointermove', trackPointer)
        return () => {
            document.removeEventListener('keydown', dismiss, true)
            document.removeEventListener('pointermove', trackPointer)
        }
    }, [visible])
    if (!content) return children

    const child = React.isValidElement(children) && children.type !== React.Fragment ? children : null
    // Disabled controls and static badges cannot receive focus themselves.
    // The wrapper exposes their help without enabling the underlying action.
    const wrapperFocus = !child || child.props.disabled || (typeof child.type === 'string'
        && !['button', 'a', 'input', 'select', 'textarea', 'summary'].includes(child.type)
        && child.props.tabIndex == null)
    const trigger = child && !wrapperFocus ? React.cloneElement(child, {
        'aria-describedby': [child.props['aria-describedby'], id].filter(Boolean).join(' '),
    }) : children

    return (
        <span ref={wrapper} className={cn("relative inline-flex rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500", wrapperClassName)}
            tabIndex={wrapperFocus ? 0 : undefined}
            aria-describedby={wrapperFocus ? [child?.props['aria-describedby'], id].filter(Boolean).join(' ') : undefined}
            onMouseEnter={() => { window.clearTimeout(leaveTimer.current); setHovered(true); setDismissed(false) }}
            onMouseLeave={() => { leaveTimer.current = window.setTimeout(() => setHovered(false), 150) }}
            onFocusCapture={() => { setFocused(true); setDismissed(false) }}
            onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false) }}>
            {trigger}
            <span
                ref={popup} id={id} role="tooltip" hidden={!visible} style={{marginLeft: offset}}
                className={cn(
                    "pointer-events-none absolute z-50 w-max max-w-[min(260px,calc(100vw-1rem))] rounded-lg bg-gray-800 dark:bg-gray-700",
                    "px-3 py-2 text-sm leading-relaxed text-white shadow-lg break-keep text-center",
                    SIDE_POSITION[side],
                    className
                )}
            >
                {content}
                <span className={cn("absolute w-0 h-0 border-[5px]", SIDE_ARROW[side])} style={{marginLeft: -offset}} />
            </span>
        </span>
    )
}

export { Tooltip }
