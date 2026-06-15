package dev.pell.intellij.symbols

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * `producesValue()` decides whether the discarded-return inspection flags
 * a bare-call statement. The subtle case is `Result<Unit, E>`: it lowers
 * to a PL/SQL PROCEDURE (the `Ok` carries no value), so `f()?;` as a
 * statement is valid — it must NOT be flagged.
 */
class PellTypeProducesValueTest {

    private fun produces(ret: String) = PellType.parse(ret).producesValue()

    @Test fun unitAndUnknownDoNotProduce() {
        assertEquals(false, produces("Unit"))
        assertEquals(false, produces(""))               // -> Unknown
    }

    @Test fun resultUnitIsProcedural() {
        // The regression: these were wrongly flagged as discarded values.
        assertEquals(false, produces("Result<Unit, JobFailed>"))
        assertEquals(false, produces("Result<Unit, A | B>"))   // union error
    }

    @Test fun realValuesProduce() {
        assertEquals(true, produces("number"))
        assertEquals(true, produces("text"))
        assertEquals(true, produces("Result<text, NotFound>"))   // Ok carries a value
        assertEquals(true, produces("Result<Employee, NotFound>"))
        assertEquals(true, produces("list<Rating>"))
        assertEquals(true, produces("Employee"))
    }
}
