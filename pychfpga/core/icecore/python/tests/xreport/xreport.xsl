<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet id="xslt" version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
    <!-- Root element: emit HTML boilerplate -->
    <xsl:template match="root">
        <html>
            <head>
                <meta charset="utf-8" />
                <meta http-equiv="X-UA-Compatible" content="IE=edge" />
                <title><xsl:value-of select="group/title" /></title>

                <link rel="stylesheet"
                    href="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.1/css/bootstrap.min.css" />
            </head>

            <body>
                <div id="synopsis_data" style="display:none"><xsl:value-of select="group/synopsis_data" /></div>

                <div class="container" role="main">
                    <!-- Activate templates. -->
                    <xsl:apply-templates />
                </div>

                <script src="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.1/js/bootstrap.min.js"></script>
            </body>
        </html>
    </xsl:template>

    <!-- Group entries: generate a block containing name, description, TOC;
         then, recurse. -->
    <xsl:template match="group">
        <!-- Title (also a link anchor) -->
        <div class="page-header">
            <h1>
                <xsl:attribute name="id">ref_<xsl:number format="1" level="multiple" count="group|case" /></xsl:attribute>
                <xsl:value-of select="title"/> <!--! (<xsl:value-of select="testname" />)-->
            </h1>
        </div>

        <!-- Description (copies entire subtree) -->
        <xsl:copy-of select="description/*" />

        <!-- Context table, if provided -->
        <xsl:if test="context">
            <h2>Target and Test Information</h2>
            <table class="table table-striped table-bordered">
                <thead>
                    <tr>
                        <th class="col-md-6">Test Suite</th>
                        <th class="col-md-6">Result</th>
                    </tr>
                </thead>
                <tbody>
                    <xsl:for-each select="context/*">
                        <tr>
                            <td><xsl:value-of select="name" /></td>
                            <td><xsl:value-of select="value" /></td>
                        </tr>
                    </xsl:for-each>
                </tbody>
            </table>
        </xsl:if>

        <!-- Summary table -->
        <h2>Summary</h2>
        <table class="table table-striped table-bordered">
            <thead>
                <tr>
                    <th class="col-md-6">Test Suite</th>
                    <th class="col-md-6">Result</th>
                </tr>
            </thead>
            <tbody>
                <xsl:for-each select="group|case">
                    <tr>
                        <!-- Suite name. Emit a link if appropriate. -->
                        <td>
                            <xsl:choose>
                                <xsl:when test="name()='group' or details">
                                    <a>
                                        <xsl:attribute name="href">
                                            #ref_<xsl:number format="1" level="multiple" count="group|case" />
                                        </xsl:attribute>
                                        <xsl:value-of select="title" />
                                    </a>
                                </xsl:when>
                                <xsl:otherwise>
                                    <xsl:value-of select="title" />
                                </xsl:otherwise>
                            </xsl:choose>
                        </td>
                        <!-- Pass/fail/unknown block -->
                        <xsl:choose>
                            <xsl:when test="passed='true'">
                                <td class="success">
                                    Passed
                                    <xsl:if test="summary">
                                        (<xsl:value-of select="summary" />)
                                    </xsl:if>
                                </td>
                            </xsl:when>
                            <xsl:when test="passed='false'">
                                <td class="danger">
                                    <strong>Failed</strong>
                                    <xsl:if test="summary">
                                        (<xsl:value-of select="summary" />)
                                    </xsl:if>
                                </td>
                            </xsl:when>
                            <xsl:otherwise>
                                <td class="danger">
                                    <strong>Unspecified</strong>
                                    <xsl:if test="summary">
                                        (<xsl:value-of select="summary" />)
                                    </xsl:if>
                                </td>
                            </xsl:otherwise>
                        </xsl:choose>
                    </tr>
                </xsl:for-each>
            </tbody>
        </table>

        <!-- Details (copies entire subtree) -->
        <xsl:if test="details">
            <h2>Details</h2>
            <xsl:copy-of select="details/*" />
        </xsl:if>

        <!-- Emit any subelements -->
        <xsl:apply-templates />
        <hr></hr>

    </xsl:template>

    <!-- Test Case: IFF there are details, emit a block. -->
    <xsl:template match="case[details]">

        <!-- Title and link anchor (for TOC links) -->
        <h3>
            <xsl:attribute name="id">ref_<xsl:number format="1" level="multiple" count="group|case" /></xsl:attribute>
            <xsl:value-of select="title"/><!--(<xsl:value-of select="testname" />)-->
        </h3>

        <!-- Description -->
        <xsl:copy-of select="description/*" />

        <!-- Details -->
        <xsl:copy-of select="details|figure" />
        <!-- <xsl:copy-of select="details/*" /> -->
    </xsl:template>

    <!-- Generic table -->
    <xsl:template match="table">
        <table class="table table-striped table-bordered">
            <thead>
                <xsl:for-each select="thead">
                    <tr>
                        <td><xsl:value-of select="tcell" /></td>
                    </tr>
                </xsl:for-each>
            </thead>
            <tbody>
                <xsl:for-each select="tbody">
                    <tr>
                        <td><xsl:value-of select="tcell" /></td>
                    </tr>
                </xsl:for-each>
            </tbody>
        </table>
    </xsl:template>

    <!-- Suppress default rule -->
    <xsl:template match="*" />
</xsl:stylesheet>

<!-- vim: set sts=4 ts=4 sw=4 tw=78 smarttab expandtab: -->
