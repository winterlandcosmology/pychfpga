#ifndef __SUPPORT_H__
#define __SUPPORT_H__

#include <strings.h>
#include <boost/preprocessor.hpp>

/* VALIDATE_COND */

#define VALIDATE_ASSERT(assertion, return_value) ({ \
	if(!(assertion)) {							\
		oops("Assertion " BOOST_PP_STRINGIZE(assertion) " failed!");	\
		return return_value;						\
	}									\
})


/*
 * VALIDATE_BETWEEN
 */

/* The condition, here, is written to catch NaN values as well. Resist the
 * temptation to write a simpler condition that fails boundary cases. */
#define VALIDATE_BETWEEN(c, lower, upper, return_value) ({			\
	__typeof__(c) __val = (c);						\
	if(!(__val >= (lower) && __val <= (upper))) {			\
		oops("Parameter " BOOST_PP_STRINGIZE(c) " out of range ["	\
			BOOST_PP_STRINGIZE(lower)				\
			", "							\
			BOOST_PP_STRINGIZE(upper)				\
			"]!");							\
		return return_value;						\
	}									\
})

/*
 * VALIDATE_STRING_MATCH: ensures a string is a case-insensitive match to a
 * number of possibilities.
 */

#define VALIDATE_STRINGS(u, num_allowed, allowed, return_value)			\
	VALIDATE_STRINGS_LIST(u, BOOST_PP_TUPLE_TO_LIST(num_allowed, allowed), return_value)

#define VALIDATE_STRINGS_LIST(u, allowed, return_value) ({			\
	__typeof__(u) __val = (u);							\
	if(!__val || !(BOOST_PP_LIST_FOLD_LEFT(VALIDATE_STRINGS_fold, 0, allowed))) {	\
		oops("Parameter " BOOST_PP_STRINGIZE(u) ": invalid value '%s'."	\
			"Valid entries are in the set ["			\
			BOOST_PP_LIST_FOLD_LEFT(VALIDATE_STRINGS_fold2, , allowed)\
			"]. Comparison is case-insensitive.",			\
			__val);							\
		return return_value;						\
	}									\
})

#define VALIDATE_STRINGS_fold(d, state, x) state || !strcasecmp(__val, x)
#define VALIDATE_STRINGS_fold2(d, state, x) state ", " BOOST_PP_STRINGIZE(x)

int fdscanf(int fd, const char *fmt, ...);

#endif /* __SUPPORT_H__ */
