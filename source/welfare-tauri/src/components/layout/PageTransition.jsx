import { motion, useReducedMotion } from 'framer-motion';

const PageTransition = ({ children }) => {
    const reducedMotion = useReducedMotion();
    return (
        <motion.div
            initial={reducedMotion ? false : { opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reducedMotion ? { opacity: 1, y: 0 } : { opacity: 0, y: -20 }}
            transition={{ duration: reducedMotion ? 0 : 0.3, ease: "easeInOut" }}
        >
            {children}
        </motion.div>
    );
};

export default PageTransition;
